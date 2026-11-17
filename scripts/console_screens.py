# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Render AutoHelix's console screens from real mock-agent runs, as text and PNG.

    uv run --no-project --with pyte --with rich python scripts/console_screens.py \
        --python .venv/bin/python [--out screens] [--widths 80,120]

Runs the CLI of the checkout this script is in (its `src/` first on PYTHONPATH),
with the interpreter given by --python, under a pseudo-terminal of each width:
`run` (live and final frames), `resume`, `view ID --once`, `view --once` and
bare `autohelix` (the entry screen: with runs, with a config and no runs, with
nothing, with two agents, outside git), plus `run` and bare `autohelix` piped (no
TTY). A fake `claude` (and `codex`) prints the session it was started with. The projects are
throwaway folders with the mock agent: no model calls. Each screen is saved as
<name>-<width>.txt and .png (`rsvg-convert` from the SVG; skipped without it);
README.md lists them and any scenario that failed. Always exits 0.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import textwrap
import time
from pathlib import Path

import pyte
from rich.console import Console
from rich.style import Style
from rich.text import Text

REPO = Path(__file__).resolve().parent.parent
ROWS = 200  # tall enough that nothing scrolls off: a screen is the whole transcript

# Iteration n of the mock agent appends `ITER = n` to sort.py. Iteration 2 fails
# the gate, 4 is slower than 3 (rejected by the metric gate), the others improve.
SPEED = {0: 46.9, 1: 17481.5, 2: 20000.0, 3: 35310.7, 4: 33002.0, 5: 57520.8, 6: 61004.2}
MEM = {0: 120, 1: 118, 2: 119, 3: 96, 4: 140, 5: 95, 6: 94}

FILES = {
    "sort.py": "ITER = 0\n",
    "check.py": textwrap.dedent("""\
        import os, pathlib, re, sys
        n = int(re.findall(r"ITER = (\\d+)", pathlib.Path("sort.py").read_text())[-1])  # the mock appends
        mark = os.environ.get("SCREENS_MARK")
        if mark:
            with open(mark, "a") as f:
                f.write(f"{n}\\n")
        if n == 2:
            print("FAILED test_sort.py::test_negative_numbers - assert [-1, -3] == [-3, -1]")
            sys.exit(1)
        print("4 passed")
        """),
    "bench.py": textwrap.dedent(f"""\
        import pathlib, re
        n = int(re.findall(r"ITER = (\\d+)", pathlib.Path("sort.py").read_text())[-1])  # the mock appends
        speed, mem = {SPEED!r}, {MEM!r}
        print(f"##autohelix[speed={{speed[n]}}]")
        print(f"##autohelix[mem_mb={{mem[n]}}]")
        """),
}


def config() -> str:
    return textwrap.dedent(f"""\
        goal: |
          Make sort_list faster in sort.py.
        scope:
          editable: [sort.py]
        gates:
          - python3 check.py
          - metric: speed
            regress: 5%
        observe:
          - name: bench
            command: python3 bench.py
            metrics:
              speed: higher
              mem_mb: lower
        agent:
          backend: mock
          timeout: 30m
          settings:
            change_file: sort.py
            change_content: "ITER = {{iteration}}\\n"
            cost_usd: 0.31
            sleep_seconds: 2.5
        """)


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def make_project(root: Path, name: str, scenario: bool) -> Path:
    project = root / name
    project.mkdir()
    git(project, "init", "-q", "-b", "main")
    if scenario:
        for path, text in FILES.items():
            (project / path).write_text(text)
        (project / "autohelix.yaml").write_text(config())
    else:
        (project / "README.md").write_text("# sorting\n")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "init")
    return project


def environment(root: Path, width: int, agents=("claude",)) -> dict:
    """A clean environment: none of an enclosing run's AUTOHELIX_*/GIT_* variables,
    fake `agents` on PATH (bare `autohelix` offers them; one started prints its
    arguments: the first prompt), our own copies."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AUTOHELIX_", "GIT_"))}
    fake = root / ("bin-" + "-".join(agents))
    if not fake.exists():
        fake.mkdir()
        for agent in agents:
            (fake / agent).write_text(f"#!/bin/sh\necho '(a fake {agent}, started with:)'\n"
                                      "for a in \"$@\"; do printf '[%s]\\n' \"$a\"; done\n")
            (fake / agent).chmod(0o755)
    env.update(
        PATH=f"{fake}:/usr/bin:/bin:/usr/sbin:/sbin",
        PYTHONPATH=os.pathsep.join([str(REPO / "src"), *filter(None, [env.get("PYTHONPATH")])]),
        AUTOHELIX_COPIES_ROOT=str(root / "copies"),
        GIT_AUTHOR_NAME="screens", GIT_AUTHOR_EMAIL="screens@example.com",
        GIT_COMMITTER_NAME="screens", GIT_COMMITTER_EMAIL="screens@example.com",
        TERM="xterm-256color", COLORTERM="truecolor", COLUMNS=str(width), LINES=str(ROWS),
    )
    env.pop("NO_COLOR", None)
    env.pop("FORCE_COLOR", None)
    return env


class Screen(pyte.Screen):
    """pyte drops SGR 2 (faint), which Rich uses for dim text: keep it as blink (5), which
    Rich's output doesn't otherwise use, and render blink as dim."""

    def select_graphic_rendition(self, *attrs, **kwargs):
        mapped, rest = [], list(attrs)
        while rest:
            a = rest.pop(0)
            if a in (38, 48) and rest:  # extended colour: 5;n or 2;r;g;b, passed through
                count = 2 if rest[0] == 5 else 4
                mapped += [a, *rest[:count]]
                rest = rest[count:]
            elif a == 2:
                mapped.append(5)
            elif a == 22:
                mapped += [22, 25]
            else:
                mapped.append(a)
        super().select_graphic_rendition(*mapped, **kwargs)


def run_in_pty(argv, cwd, env, width, *, keys=(), frame_when=None, timeout=180):
    """Run argv under a pty of `width` columns; return (final screen, live frame, exit code).

    keys: (when, bytes), sent in order: `when` is a delay in seconds after the previous
    key (the first: after the start), or text to wait for on the screen (the entry
    screen drops keys typed before its menu is drawn). frame_when: a function of the
    screen's text returning True when the live frame should be taken (checked between
    reads)."""
    screen = Screen(width, ROWS)
    stream = pyte.ByteStream(screen)
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execve(argv[0], argv, env)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, width, 0, 0))
    start, live, pending = time.monotonic(), None, list(keys)
    code, last_key = None, start
    while True:
        elapsed = time.monotonic() - start
        while pending and (pending[0][0] in plain(snapshot(screen)) if isinstance(pending[0][0], str)
                           else time.monotonic() - last_key >= pending[0][0]):
            os.write(fd, pending.pop(0)[1])
            last_key = time.monotonic()
        if elapsed > timeout:
            os.kill(pid, 9)
        ready, _, _ = select.select([fd], [], [], 0.05)
        if ready:
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            if not data:
                break
            stream.feed(data)
        if live is None and frame_when and frame_when(plain(snapshot(screen))):
            live = snapshot(screen)
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            code = os.waitstatus_to_exitcode(status)
            while select.select([fd], [], [], 0.1)[0]:  # drain what's left
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                stream.feed(data)
            break
    os.close(fd)
    if code is None:
        code = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
    return snapshot(screen), live, code


MENU = "(q: quit)"  # the last line the entry screen's menu draws


def showing(*texts: str):
    """A `frame_when` that is true once the screen shows all of `texts`."""
    return lambda text: all(t in text for t in texts)


def snapshot(screen: pyte.Screen) -> list[list]:
    lines = [[screen.buffer[y][x] for x in range(screen.columns)] for y in range(screen.lines)]
    while lines and all(c.data == " " for c in lines[-1]):
        lines.pop()
    return lines


COLOURS = {"brown": "yellow", "default": None}


def colour(name: str):
    if name in COLOURS:
        return COLOURS[name]
    if re.fullmatch(r"[0-9a-fA-F]{6}", name):
        return f"#{name}"
    return name.replace("bright", "bright_") if name.startswith("bright") and "_" not in name else name


def to_text(lines) -> Text:
    rows = []
    for row in lines:
        text = Text()
        for c in row:
            fg, bg = colour(c.fg), colour(c.bg)
            if c.reverse:
                fg, bg = bg or "black", fg or "white"
            try:
                style = Style(color=fg, bgcolor=bg, bold=c.bold, italic=c.italics,
                              underline=c.underscore, dim=c.blink)  # blink carries faint, see Screen
            except Exception:
                style = Style()
            text.append(c.data or " ", style)
        text.rstrip()
        rows.append(text)
    return Text("\n").join(rows)


def plain(lines) -> str:
    return "\n".join("".join(c.data for c in row).rstrip() for row in lines) + "\n"


def fix_svg(svg: str) -> str:
    """rsvg mis-measures &#160;: move leading/trailing spaces into x / textLength."""
    def fix(m):
        attrs, body = m[1], m[2]
        if "textLength" not in attrs:
            return m[0]
        s = body.replace("&#160;", " ")
        x0 = float(re.search(r' x="([\d.]+)"', attrs)[1])
        length = float(re.search(r'textLength="([\d.]+)"', attrs)[1])
        cw = length / max(len(s), 1)
        lead = len(s) - len(s.lstrip(" "))
        core = s.strip(" ")
        if not core:
            return ""
        attrs = re.sub(r' x="[\d.]+"', f' x="{x0 + lead * cw:.1f}"', attrs)
        attrs = re.sub(r'textLength="[\d.]+"', f'textLength="{len(core) * cw:.1f}"', attrs)
        return f"{attrs}{core.replace(' ', '&#160;')}</text>"
    return re.sub(r"(<text[^>]*>)([^<]*)</text>", fix, svg)


def save(out: Path, name: str, lines, width: int, title: str) -> None:
    (out / f"{name}.txt").write_text(plain(lines))
    console = Console(record=True, width=width, force_terminal=True, color_system="truecolor",
                      file=open(os.devnull, "w"))
    console.print(to_text(lines))
    svg = out / f"{name}.svg"
    svg.write_text(fix_svg(console.export_svg(title=title)))
    if shutil.which("rsvg-convert"):
        subprocess.run(["rsvg-convert", "-z", "1.5", "-o", str(out / f"{name}.png"), str(svg)],
                       capture_output=True)
        if (out / f"{name}.png").exists():
            svg.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--python", required=True, help="interpreter with AutoHelix's dependencies")
    parser.add_argument("--out", default="screens", type=Path)
    parser.add_argument("--widths", default="80,120")
    args = parser.parse_args()
    python = str(Path(args.python).absolute())
    out = args.out.absolute()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    cli = [python, "-c", "import sys; from autohelix.cli import main; sys.argv[0] = 'autohelix'; main()"]
    done, failed = [], []

    def record(name, lines, width, title, code, expected=0):
        save(out, name, lines, width, title)
        done.append(f"- `{name}`: {title} (exit {code})")
        if code != expected:
            failed.append(f"- `{name}`: exit {code}, expected {expected}")

    for width in [int(w) for w in args.widths.split(",")]:
        with tempfile.TemporaryDirectory(prefix="autohelix-screens-") as tmp:
            root = Path(tmp)
            env = environment(root, width)
            try:
                project = make_project(root, "sorting", True)
                mark = root / "mark"
                env["SCREENS_MARK"] = str(mark)

                def mid_iteration_3(text):
                    # iteration 2's gate has run: iteration 3's agent is now working (2.5s)
                    if not mark.exists() or "2" not in mark.read_text().split():
                        return False
                    mid_iteration_3.seen = getattr(mid_iteration_3, "seen", None) or time.monotonic()
                    return time.monotonic() - mid_iteration_3.seen > 1.2

                final, live, code = run_in_pty([*cli, "run", "-n", "5"], project, env, width,
                                               frame_when=mid_iteration_3)
                if live:
                    record(f"run-live-{width}", live, width, f"autohelix run -n 5 · iteration 3 running · {width} cols", code)
                else:
                    failed.append(f"- `run-live-{width}`: no live frame taken")
                record(f"run-{width}", final, width, f"autohelix run -n 5 · {width} cols", code)

                final, _, code = run_in_pty([*cli, "resume", "-n", "1"], project, env, width)
                record(f"resume-{width}", final, width, f"autohelix resume -n 1 · {width} cols", code)
                short = max((project / ".autohelix" / "runs").iterdir()).name.rsplit("-", 1)[-1]  # the run above
                for name, command in (("view", ["view", short, "--once"]), ("dashboard", ["view", "--once"])):
                    final, _, code = run_in_pty([*cli, *command], project, env, width)
                    record(f"{name}-{width}", final, width, f"autohelix {' '.join(command)} · {width} cols", code)

                # Bare `autohelix`: the entry screen. A second run, interrupted in its first iteration.
                run_in_pty([*cli, "run", "-n", "3", "--no-report"], project, env, width,
                           keys=[(4.0, b"\x03")], timeout=60)
                # With runs, as drawn while choosing; q quits (exit 0).
                _, live, code = run_in_pty([*cli], project, env, width, keys=[(MENU, b"q")],
                                           frame_when=showing(MENU), timeout=30)
                record(f"entry-runs-{width}", live or [], width, f"autohelix · runs (then q) · {width} cols", code)
                # The same, Enter: the first option starts the (fake) agent with its first prompt.
                final, _, code = run_in_pty([*cli], project, env, width, keys=[(MENU, b"\r")], timeout=30)
                record(f"entry-ask-{width}", final, width, f"autohelix · runs, Enter · {width} cols", code)
                # Two agents: the labels say "an agent", then "Which agent?"; q there quits.
                two = environment(root, width, ("claude", "codex"))
                _, live, code = run_in_pty([*cli], project, two, width,
                                           keys=[(MENU, b"\x1b[B"), (0.5, b"\r"), ("Which agent?", b"q")],
                                           frame_when=showing("Which agent?", MENU), timeout=30)
                record(f"entry-agents-{width}", live or [], width,
                       f"autohelix · two agents, ↓ Enter (then q) · {width} cols", code)

                configs = make_project(root, "half-done", True)  # autohelix.yaml, no runs yet
                _, live, code = run_in_pty([*cli], configs, env, width, keys=[(MENU, b"q")],
                                           frame_when=showing(MENU), timeout=30)
                record(f"entry-config-{width}", live or [], width,
                       f"autohelix · a config, no runs (then q) · {width} cols", code)

                empty = make_project(root, "new-project", False)
                (empty / "src").mkdir()  # started below the top: the project is the checkout's top
                final, _, code = run_in_pty([*cli], empty / "src", env, width, keys=[("Enter/q", b"\r")],
                                            timeout=30)
                record(f"entry-new-{width}", final, width, f"autohelix · nothing yet, Enter · {width} cols", code)

                outside = root / "not-git"
                outside.mkdir()
                no_git = {**env, "GIT_CEILING_DIRECTORIES": str(root)}
                final, _, code = run_in_pty([*cli], outside, no_git, width, timeout=30)
                record(f"entry-no-git-{width}", final, width, f"autohelix · outside git · {width} cols", code, 1)
            except Exception as e:  # keep what was rendered; say what broke
                failed.append(f"- width {width}: {type(e).__name__}: {e}")

    with tempfile.TemporaryDirectory(prefix="autohelix-screens-") as tmp:  # no TTY
        root = Path(tmp)
        try:
            project = make_project(root, "sorting", True)
            env = environment(root, 100)
            result = subprocess.run([*cli, "run", "-n", "3"], cwd=project, env=env, text=True,
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=180)
            (out / "run-piped.txt").write_text(result.stdout + result.stderr)
            done.append(f"- `run-piped`: autohelix run -n 3 with output to a pipe (exit {result.returncode})")
            if result.returncode:
                failed.append(f"- `run-piped`: exit {result.returncode}")
            result = subprocess.run(cli, cwd=project, env=env, text=True, stdin=subprocess.DEVNULL,
                                    capture_output=True, timeout=60)
            (out / "entry-piped.txt").write_text(result.stdout + result.stderr)
            done.append(f"- `entry-piped`: bare autohelix with output to a pipe (exit {result.returncode})")
            if result.returncode != 2:
                failed.append(f"- `entry-piped`: exit {result.returncode}, expected 2")
        except Exception as e:
            failed.append(f"- run-piped: {type(e).__name__}: {e}")

    (out / "README.md").write_text(
        "# Console screens\n\nReal mock-agent runs of a sorting project (no model calls). The run: "
        "baseline, then 1 accepted, 2 rejected by the gate, 3 accepted, 4 rejected by the "
        "metric gate (speed 6.5% below 3), 5 accepted; resume adds 6. Each screen is a .txt "
        "and a .png.\n\n" + "\n".join(done) + "\n"
        + ("\n## Failed\n\n" + "\n".join(failed) + "\n" if failed else ""))
    print(f"{len(done)} screens in {out}" + (f"; {len(failed)} problems (see README.md)" if failed else ""))
    for line in failed:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
