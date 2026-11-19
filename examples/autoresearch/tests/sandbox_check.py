"""Run inside ar/sandbox.sh (tests/check_sandbox.sh does): checks each restriction.

    python sandbox_check.py PCI_BUS_ID HOST_DATA_DIR [PATH_THAT_MUST_BE_HIDDEN...]

Prints PASS or FAIL per check and exits 1 if any failed.
"""

import errno
import os
import socket
import subprocess
import sys

bus, host_data, *hidden = sys.argv[1:]
os.environ["CUDA_VISIBLE_DEVICES"] = "0,1,2,3,4,5,6,7"  # asks for every GPU: only one may show
home = os.environ["HOME"]
data = f"{home}/.cache/autoresearch/data"
tokenizer = f"{home}/.cache/autoresearch/tokenizer"
scratch = os.getcwd()
failed = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name}{': ' + str(detail) if detail else ''}")
    if not ok:
        failed.append(name)


def raises(action, *codes):
    """True if action() raises OSError with one of codes (any OSError if none given)."""
    try:
        action()
    except OSError as e:
        return not codes or e.errno in codes
    return False


def write(path):
    with open(path, "ab") as f:
        f.write(b"x")


def sh(command):
    return subprocess.run(["sh", "-c", command], capture_output=True, text=True)


# Network
check("network: only a loopback interface", [n for _, n in socket.if_nameindex()] == ["lo"],
      socket.if_nameindex())
check("network: no route out", raises(lambda: socket.create_connection(("1.1.1.1", 443), timeout=5)))
check("network: no DNS", raises(lambda: socket.getaddrinfo("huggingface.co", 443)))

# GPU
nodes = sorted(n for n in os.listdir("/dev") if n.startswith("nvidia"))
gpus = [n for n in nodes if n[6:].isdigit()]
check("gpu: one GPU node in /dev", len(gpus) == 1, nodes)
others = [f"/dev/nvidia{i}" for i in range(8) if f"nvidia{i}" not in gpus]
check("gpu: other GPU nodes absent", all(not os.path.exists(p) for p in others))
check("gpu: can't create a device node for another GPU",
      raises(lambda: os.mknod("/tmp/nv", 0o666 | 0o020000, os.makedev(195, int(others[0][11:]))),
             errno.EPERM))
import torch  # noqa: E402

count = torch.cuda.device_count()
check("gpu: CUDA sees one device, with CUDA_VISIBLE_DEVICES=0..7", count == 1, count)
props = torch.cuda.get_device_properties(0)
seen = f"{props.pci_domain_id:08X}:{props.pci_bus_id:02X}:{props.pci_device_id:02X}.0"
check("gpu: it is the GPU asked for", seen.upper() == bus.upper(), f"{seen} vs {bus}")
check("gpu: it computes", torch.ones(4, device="cuda").sum().item() == 4)
check("devices: no InfiniBand/EFA devices", not os.path.exists("/dev/infiniband"))

# Data: training shards and tokenizer only, read-only
shards = sorted(os.listdir(data))
check("data: the training shards only", shards == [f"shard_{i:05d}.parquet" for i in range(10)], shards)
check("data: no validation shard", not os.path.exists(f"{data}/shard_06542.parquet")
      and not os.path.exists(f"{host_data}/shard_06542.parquet"))
check("data: shards read-only", raises(lambda: write(f"{data}/{shards[0]}"), errno.EROFS))
check("data: can't add files", raises(lambda: write(f"{data}/new.parquet"), errno.EROFS))
check("data: tokenizer read-only", raises(lambda: write(f"{tokenizer}/tokenizer.pkl"), errno.EROFS))
sys.path.insert(0, scratch)
import prepare  # noqa: E402

try:
    next(prepare.make_dataloader(prepare.Tokenizer.from_directory(), 1, 64, "val"))
    check("data: the val dataloader fails", False)
except (OSError, FileNotFoundError) as e:
    check("data: the val dataloader fails", True, type(e).__name__)
batch = next(prepare.make_dataloader(prepare.Tokenizer.from_directory(), 1, 64, "train"))
check("data: the train dataloader works", tuple(batch[0].shape) == (1, 64))

# Writes: only scratch (and /tmp, which is in it)
for path in [sys.executable, f"{os.environ['HF_HOME']}/x", "/etc/x", "/usr/x", "/x", "/sys/x", "/proc/x"]:
    check(f"writes: refused at {path}", raises(lambda: write(path)))
write(f"{scratch}/ok")
write("/tmp/ok")
check("writes: scratch and /tmp work, /tmp is scratch/tmp",
      os.path.exists(f"{scratch}/tmp/ok") and os.path.exists(f"{scratch}/ok"))

# Mounts can't be undone or made writable, here or in a nested user namespace
for command in [f"umount {data}/{shards[0]}", "umount /dev/nvidiactl", f"mount -o remount,rw,bind {data}",
                f"unshare -Urm sh -c 'umount {data}/{shards[0]} || umount -l {data}'",
                f"unshare -Urm mount -o remount,rw,bind {data}",
                f"unshare -Urm sh -c 'mkdir -p /tmp/o && mount --bind / /tmp/o && ls /tmp/o/.old'"]:
    result = sh(command)
    check(f"mounts: refused: {command}", result.returncode != 0, result.stderr.strip()[-120:])


def nested_umount(paths):
    """unshare(2) a user + mount namespace (full capabilities in it, no exec to lose them),
    then try to unmount: inherited mounts are locked, so every umount must fail."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    pid = os.fork()
    if pid == 0:
        if libc.unshare(0x10000000 | 0x00020000) != 0:  # CLONE_NEWUSER | CLONE_NEWNS
            os._exit(3)
        undone = sum(libc.umount2(p.encode(), 2) == 0 for p in paths)  # MNT_DETACH
        os._exit(1 if undone else 0)
    return os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])


code = nested_umount([f"{data}/{shards[0]}", data, "/dev", "/dev/nvidiactl", "/"])
check("mounts: a nested namespace can't unmount anything (unshare(2) + umount2)", code == 0, code)
with open("/proc/self/status") as f:
    status = dict(line.split(":\t", 1) for line in f if ":\t" in line)
check("capabilities: none, no_new_privs",
      int(status["CapEff"], 16) == 0 and int(status["CapBnd"], 16) == 0 and status["NoNewPrivs"].strip() == "1")

# Nothing else visible
for path in hidden:
    check(f"hidden: {path}", not os.path.exists(path))
pids = [p for p in os.listdir("/proc") if p.isdigit()]
check("processes: only the sandbox's", len(pids) < 10 and "1" in pids, pids)
check("home: HOME is in scratch", home.startswith(scratch))

print(f"{len(failed)} failed" if failed else "all passed")
sys.exit(1 if failed else 0)
