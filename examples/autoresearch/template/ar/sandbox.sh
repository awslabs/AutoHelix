#!/bin/bash
# Run a command for training, confined to one GPU and a scratch folder.
#
#   AR_GPU=K bash ar/sandbox.sh SCRATCH COMMAND [ARGS...]
#
# K is the GPU's index as nvidia-smi numbers it. COMMAND runs in SCRATCH (its working
# directory) inside new user, mount, network, PID, IPC and UTS namespaces (`unshare`, no
# root needed), on a new root (`pivot_root`) that holds only:
# - /usr and /etc, read-only, submounts too, and the /bin, /lib, /lib64, /sbin links;
# - the venv, its Python and the FlashAttention-3 cache (ar/paths.sh), read-only;
# - a /dev of null, zero, full, random, urandom, tty, a private /dev/shm, and of the GPU
#   devices nvidiactl, nvidia-uvm, nvidia-uvm-tools and GPU K's node only;
# - a fresh /proc, listing only the sandbox's processes; /sys, read-only, submounts too;
# - SCRATCH, writable, at /scratch (the working directory); /tmp is SCRATCH/tmp; HOME is
#   /scratch/home, where ~/.cache/autoresearch/data holds every training shard (all but
#   the validation shard, shard_06542) and ~/.cache/autoresearch/tokenizer the tokenizer,
#   read-only: where prepare.py looks for them. AR_EXPORT is /scratch/model.pt2, so the
#   export (ar.export.save) is SCRATCH/model.pt2 outside.
# The old root is detached, so nothing else is reachable, and nothing sensitive lies under
# a mount. Then every capability is dropped (setpriv, no_new_privs): mounts can't be
# undone or made writable. The network namespace has only a loopback device, which is
# down. The environment is reset to PATH, HOME, TMPDIR, the HF cache settings, AR_SEED
# and AR_EXPORT.
#
# In an arm's container (arms/container/) it runs as user ar, the only one besides root
# that may open the GPU devices; the container needs seccomp=unconfined (for unshare)
# and systempaths=unconfined (a fresh /proc can't be mounted under Docker's masked one).
#
# The command's first process is the namespace's init: when it exits, or when `unshare`
# is killed (by a timeout), the kernel kills every process in the sandbox.
#
# Not covered: CPU and memory use, GPU memory, the time (the caller's limit), what the
# command writes to SCRATCH, /dev/shm and stdout, and what it reads in /usr, /etc and /sys.
# tests/check_sandbox.sh checks each restriction on a machine.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$here/paths.sh"
VAL_SHARD=shard_06542.parquet

ro_remount() {  # ro_remount TARGET: TARGET and every mount below it read-only
    local mounts target opts
    # Each keeps its other flags (nosuid, noexec, atime...): a user namespace may not clear them.
    mounts=$(findmnt -Rrno TARGET,VFS-OPTIONS "$1")
    while read -r target opts; do
        mount -o "remount,bind,ro${opts#r[ow]}" "$target"
    done <<< "$mounts"
}

ro_bind() {  # ro_bind SOURCE TARGET [--rbind]: a read-only bind, with its submounts if --rbind
    if [ -d "$1" ]; then mkdir -p "$2"; else touch "$2"; fi
    mount "${3:---bind}" "$1" "$2"
    ro_remount "$2"
}

if [ "${1:-}" != --inside ]; then
    if [ $# -lt 2 ]; then
        echo "usage: AR_GPU=K bash ar/sandbox.sh SCRATCH COMMAND [ARGS...]" >&2
        exit 2
    fi
    if [ -z "${AR_GPU:-}" ]; then
        echo "sandbox.sh: set AR_GPU to the index (as nvidia-smi numbers them) of the GPU to use" >&2
        exit 2
    fi
    minor=$(nvidia-smi -q -i "$AR_GPU" | awk '/Minor Number/ {print $4}')
    if ! [[ "$minor" =~ ^[0-9]+$ ]]; then
        echo "sandbox.sh: no GPU $AR_GPU (nvidia-smi -q -i $AR_GPU)" >&2
        exit 2
    fi
    scratch=$(realpath "$1")
    shift
    mkdir -p "$scratch"/home/.cache/autoresearch/{data,tokenizer} "$scratch/tmp" "$scratch/.root"
    exec unshare --user --map-root-user --mount --net --pid --ipc --uts --fork --kill-child=SIGKILL \
        /bin/bash "$here/sandbox.sh" --inside "$scratch" "$minor" "$@"
fi

# Inside the namespaces: build the new root, enter it, drop every capability, run.
scratch=$2 minor=$3
shift 3
root=$scratch/.root
mount -t tmpfs -o mode=755 tmpfs "$root"
# Recursive: in a container, the driver's files are bound into /usr and Docker's hosts,
# hostname and resolv.conf into /etc (and a user namespace can't bind a folder without the
# mounts in it).
ro_bind /usr "$root/usr" --rbind
ro_bind /etc "$root/etc" --rbind
for link in bin sbin lib lib64; do
    ln -s "$(readlink "/$link")" "$root/$link"
done
for path in "$AR_VENV" "$AR_PYTHON_HOME" "$AR_HF_HOME"; do
    ro_bind "$path" "$root$path"
done

mkdir "$root/dev"
mount -t tmpfs -o mode=755,nosuid tmpfs "$root/dev"
for node in null zero full random urandom tty nvidiactl nvidia-uvm nvidia-uvm-tools "nvidia$minor"; do
    touch "$root/dev/$node"
    mount --bind "/dev/$node" "$root/dev/$node"
done
mkdir "$root/dev/shm"
mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs "$root/dev/shm"
ln -s /proc/self/fd "$root/dev/fd"
ln -s /proc/self/fd/0 "$root/dev/stdin"
ln -s /proc/self/fd/1 "$root/dev/stdout"
ln -s /proc/self/fd/2 "$root/dev/stderr"

mkdir "$root/proc" "$root/sys" "$root/tmp"
mount -t proc -o nosuid,nodev,noexec proc "$root/proc"
ro_bind /sys "$root/sys" --rbind

# At a fixed path, not its own: a scratch under /tmp would be hidden by the /tmp bind.
mkdir "$root/scratch"
mount --bind "$scratch" "$root/scratch"
mount --bind "$scratch/tmp" "$root/tmp"
cache=$root/scratch/home/.cache/autoresearch
mount -t tmpfs -o mode=755 tmpfs "$cache/data"
for shard in "$AR_DATA"/shard_*.parquet; do
    name=$(basename "$shard")
    [[ "$name" =~ ^shard_[0-9]{5}\.parquet$ && "$name" != "$VAL_SHARD" ]] || continue
    ro_bind "$shard" "$cache/data/$name"
done
mount -o remount,bind,ro "$cache/data"
ro_bind "$AR_TOKENIZER" "$cache/tokenizer"

mkdir "$root/.old"
cd "$root"
pivot_root . .old
cd /
umount -l /.old
rmdir /.old
mount -o remount,bind,ro tmpfs /  # a source, so mount(8) skips /etc/fstab
cd /scratch
exec env -i PATH="$AR_VENV/bin:/usr/bin" HOME=/scratch/home TMPDIR=/tmp LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 HF_HOME="$AR_HF_HOME" HF_HUB_OFFLINE=1 AR_EXPORT=/scratch/model.pt2 \
    ${AR_SEED:+AR_SEED="$AR_SEED"} \
    setpriv --no-new-privs --inh-caps=-all --ambient-caps=-all --bounding-set=-all -- "$@"
