# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""`autohelix view --web` / `autohelix dashboard --web`: a small live web app, served on 127.0.0.1.

Two pages share one server: `/` lists the project's runs, `/run/<id>/` shows one
run. The pages poll a few JSON routes. There are no write routes. Every file read
is derived from a run id that must name an existing run directory, an iteration
number, a file name that must match an entry in notes/, or a relative path inside
one iteration's observations/iter-N/ folder. Run state is opened one component at
a time from the run folder down, without following symlinks or blocking on FIFOs
(`_open_dir`, `_open_file`). Git only gets object names from history records that
are plain hex, and its diff output is fixed whatever the user's diff settings and
cut at MAX_TEXT (`RunData._run_diff`). The unprefixed `/api/...` routes serve the
server's default run.

`write_static` saves the same page as one file, output/report.html, with the
route data built in.
"""

import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import threading
import time
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from markdown_it import MarkdownIt

from autohelix.agents import AgentEventKind
from autohelix.config import ConfigNotFound, MetricGate, find_config, load_config
from autohelix.costs import summarize_costs
from autohelix.history import History
from autohelix.runs import RunPaths, config_relative, git, is_archived, last_activity
from autohelix.replay import LogTail
from autohelix.report import read_report_info
from autohelix.run_status import read_run_status
from autohelix.state import read_stored_config_path
from autohelix.workspace import ITERATION_DIR

MAX_TEXT = 256 * 1024
MAX_EVENTS = 3000  # merged events kept per log; tool output is clipped to keep this small
MAX_ARTIFACT = 20 * 1024 * 1024
MAX_FILES = 500  # captured files listed per observe entry
COMMIT = re.compile(r"[0-9a-f]{7,64}")
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}
LOOPBACK = {"127.0.0.1", "localhost", "[::1]"}
# Phases after the agent copy is frozen, when the pin holds this iteration's tree.
FROZEN_PHASES = {"preparing checks", "checks", "benchmark", "review", "accept", "stop check"}
ASSETS = {
    "/": ("dashboard.html", "text/html; charset=utf-8"),
    "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
    "/viewer.css": ("viewer.css", "text/css; charset=utf-8"),
    "/viewer.js": ("viewer.js", "text/javascript; charset=utf-8"),
    "/logo-light.svg": ("autohelix-light.svg", "image/svg+xml"),
    "/logo-dark.svg": ("autohelix-dark.svg", "image/svg+xml"),
}
# Raw HTML in notes and review output is shown as text, never rendered.
_MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
CSP = "default-src 'self'; object-src 'none'; frame-ancestors 'none'"
# report.html, opened from disk: its data and code are inline; it loads nothing.
STATIC_CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:"
# The lines `autohelix review` prints for its numbers. The review's own prose has
# them escaped ("##autohelix ["), so only the command's lines match.
METRIC_LINE = re.compile(r"^[ \t]*##autohelix\[[^\]\n]*\][ \t]*(?:\n|$)", re.MULTILINE)
OUTPUT_FILES = ("stdout.txt", "stderr.txt")  # an observe entry's own output, beside its captures


def _markdown(text: str | None) -> str | None:
    return _MARKDOWN.render(text) if text else None


def review_markdown(text: str | None) -> str | None:
    """A review entry's stdout as HTML, without its `##autohelix[...]` metric lines."""
    return _markdown(METRIC_LINE.sub("", text).strip()) if text else None


def _field(record, name: str):
    """A history field that older records (and older `IterationResult`s) lack."""
    return getattr(record, name, None) if record is not None else None


def _object(value) -> str | None:
    """A git object name from a history record, or None: never an option or a rev expression."""
    return value if isinstance(value, str) and COMMIT.fullmatch(value) else None


_OPEN = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _bad_parts(parts) -> bool:
    return any(not isinstance(part, str) or part in ("", ".", "..") or "/" in part or "\0" in part
               for part in parts)


def _contained(root: Path, parts) -> list[str] | None:
    """`parts` as the components of a normalized path that stays below `root`, or None.
    `_bad_parts` already refuses anything that could leave `root`; this is the same
    check in the normalize-then-prefix form static analysis recognizes."""
    if _bad_parts(parts):
        return None
    base = os.path.normpath(str(root))
    full = os.path.normpath(os.path.join(base, *parts))
    if full != base and not full.startswith(base + os.sep):
        return None
    relative = os.path.relpath(full, base)
    return [] if relative == "." else relative.split(os.sep)


def _open_dir(root: Path, parts) -> int | None:
    """A descriptor for the folder `root/parts...`, or None. `root`'s last component and
    every part below it are opened without following symlinks, each relative to the
    one before: no `..`, no absolute paths, nothing outside `root`."""
    parts = _contained(root, parts)
    if parts is None:
        return None
    fd = None
    try:
        fd = os.open(root, _OPEN | os.O_DIRECTORY)
        for part in parts:
            child = os.open(part, _OPEN | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError:
        if fd is not None:
            os.close(fd)
        return None


def _open_file(root: Path, parts, single_link: bool = False) -> int | None:
    """A read descriptor for the regular file `root/parts...` (opened as `_open_dir`
    opens folders), or None: never a symlink, FIFO or device, and with `single_link`
    never a file with other hard links (one planted to point at a file elsewhere)."""
    parts = _contained(root, parts) if parts else None
    if not parts:
        return None
    parent = _open_dir(root, parts[:-1])
    if parent is None:
        return None
    try:
        fd = os.open(parts[-1], _OPEN | os.O_NONBLOCK, dir_fd=parent)
    except OSError:
        return None
    finally:
        os.close(parent)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or (single_link and info.st_nlink != 1):
        os.close(fd)
        return None
    return fd


def _read_bytes(root: Path, parts, limit: int, single_link: bool = False) -> bytes | None:
    """The whole file, through `_open_file`; None if it is larger than `limit`."""
    fd = _open_file(root, parts, single_link)
    if fd is None:
        return None
    with os.fdopen(fd, "rb") as f:
        return None if os.fstat(f.fileno()).st_size > limit else f.read(limit + 1)


def _tail(fd: int) -> str:
    """A text file's content, keeping the tail if it is very large."""
    with os.fdopen(fd, "rb") as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - MAX_TEXT))
        text = f.read(MAX_TEXT).decode("utf-8", errors="replace")
    return text if size <= MAX_TEXT else "[… earlier content omitted …]\n" + text


def _read_text_at(root: Path, parts, single_link: bool = False) -> str | None:
    """`_tail` of `root/parts...`, through `_open_file`."""
    fd = _open_file(root, parts, single_link)
    return None if fd is None else _tail(fd)


def _read_user_file(path: Path) -> str | None:
    """`_tail` of a file the user owns (the config: a symlink to it is fine), never
    blocking on a FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return None
    return _tail(fd)


def _listing(root: Path, parts) -> list[tuple[str, os.stat_result]]:
    """The entries of the folder `root/parts...` (opened as `_open_dir` does), with
    their own lstat."""
    fd = _open_dir(root, parts)
    if fd is None:
        return []
    out = []
    try:
        for name in os.listdir(fd):
            try:
                out.append((name, os.stat(name, dir_fd=fd, follow_symlinks=False)))
            except OSError:
                continue
    except OSError:
        pass
    finally:
        os.close(fd)
    return sorted(out)


def _tree_files(root: Path, parts, skip: tuple[str, ...] = ()) -> list[dict]:
    """Regular single-link files below the folder `root/parts...` (opened as `_open_dir`
    does) as `{"path": "a/b.txt", "size"}`, at most MAX_FILES, walked through
    descriptors without following symlinks; `skip` names at the top are left out."""
    fd = _open_dir(root, parts)
    if fd is None:
        return []
    out: list[dict] = []
    try:
        for top, dirs, names, top_fd in os.fwalk(".", dir_fd=fd, follow_symlinks=False):
            keep = []
            for name in sorted(dirs):
                try:
                    if stat.S_ISDIR(os.stat(name, dir_fd=top_fd, follow_symlinks=False).st_mode):
                        keep.append(name)
                except OSError:
                    pass
            dirs[:] = keep
            for name in sorted(names):
                if top == "." and name in skip:
                    continue
                try:
                    info = os.stat(name, dir_fd=top_fd, follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    out.append({"path": os.path.normpath(os.path.join(top, name)), "size": info.st_size})
                    if len(out) >= MAX_FILES:
                        return out
    finally:
        os.close(fd)
    return out


DIFF_ARGS = ("diff", "--no-ext-diff", "--no-textconv", "--no-color", "--src-prefix=a/", "--dst-prefix=b/",
             "--no-relative")
# The user's diff settings must not change the text the page parses.
DIFF_CONFIG = ("-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false", "-c", "color.ui=never",
               "-c", "color.diff=never", "-c", "diff.suppressBlankEmpty=false")
DIFF_CACHE = 16  # diffs kept per run (each at most MAX_TEXT)
DIFF_CUT = "[… diff cut at {} KB …]"


def _note_order(name: str):
    match = re.fullmatch(r"iter-(\d+)\.md", name)
    return (0, int(match[1]), "") if match else (1, 0, name)


class RunData:
    """Reads one project's .autohelix/ directory for the viewer."""

    def __init__(self, project: Path, run_id: str | None = None):
        self.project = project.resolve()
        self.run_id = run_id
        self.paths = RunPaths(self.project, run_id)
        self.root = self.paths.run_dir
        self.history = History(self.project, self.paths.run_id)
        self._logs: dict[Path, tuple[LogTail, deque]] = {}
        # Diffs by their two objects, most recent last (`_cached_diff`). Diffs run git
        # outside the server's lock, so the cache and a live copy's index have their own.
        self._diffs: OrderedDict[str, tuple[str | None, str | None]] = OrderedDict()
        self._diff_lock = threading.Lock()
        self._live_lock = threading.Lock()
        self._history_lock = threading.Lock()
        self._clipped: set[Path] = set()  # logs whose oldest events were dropped
        self._subject_cache: dict[str, str | None] = {}
        self._repository = None
        self._configs: dict[Path, tuple[tuple, object, str | None]] = {}  # path -> (stat key, config, text)

    def _refresh_current(self):
        if self.run_id in (None, "current"):
            paths = RunPaths(self.project)
            if paths.run_id != self.paths.run_id:
                self.paths = paths
                self.root = paths.run_dir
                self.history = History(self.project, paths.run_id)
                self._logs.clear()
                with self._diff_lock:
                    self._diffs.clear()

    def records(self):
        """The run's history (diffs read it outside the server's lock)."""
        with self._history_lock:
            return self.history.load()

    def _config(self):
        """The run's stored config, parsed again only when its file changes. A run whose
        file is gone shows it missing, never another; with no run, the project's config
        if the lookup finds one, else none (None, None)."""
        if self.paths.run_id:
            path = read_stored_config_path(self.project, self.paths.run_id)
        else:
            try:
                path = find_config(self.project)
            except ConfigNotFound:
                return None, None
        try:
            info = path.stat()
            key = (info.st_mtime_ns, info.st_size, info.st_ino)
        except OSError:
            key = None
        cached = self._configs.get(path)
        if cached is None or cached[0] != key or key is None:
            try:
                config = load_config(self.project, config_file=path)[0]
            except Exception:
                config = None
            cached = self._configs[path] = (key, config, _read_user_file(path))
        return cached[1], path

    def _config_summary(self, config, path: Path | None) -> dict | None:
        if path is None:
            return None
        cached = self._configs.get(path)
        summary = {"file": config_relative(self.project, path), "text": cached[2] if cached else _read_user_file(path)}
        if not path.exists():
            summary["missing"] = True
        if config:
            summary.update(
                agent=" · ".join(filter(None, [config.agent.backend, config.agent.model])),
                gates=[gate.text for gate in config.gates],
                observe=[{"name": entry.name, "review": entry.review is not None, "metrics": entry.metrics}
                         for entry in config.observe],
                scope=config.editable,
                max_iterations=config.max_iterations,
                max_cost_usd=config.max_cost_usd,
                max_time_seconds=config.max_time_seconds,
            )
        return summary

    def iterations(self, records) -> list[dict]:
        """One row per iteration (`view --json`'s `iterations`, add-only).

        `summary` is the agent's one-line summary; records without one fall back to the
        commit subject, or the rejection reason. `reason` is why it was rejected."""
        subjects = self._subjects([r.commit for r in records if r.commit])
        rows = []
        for r in records:
            reason = r.reason if r.reason != "baseline" else None
            summary = _field(r, "summary")
            rows.append({
                "iteration": r.iteration, "accepted": r.accepted, "metrics": r.metrics,
                "summary": (summary if isinstance(summary, str) and summary.strip() else None)
                           or reason or subjects.get(r.commit),
                "reason": reason,
                "cost": r.cost.cost_usd, "cost_incomplete": r.cost.incomplete,
                "seconds": (r.usage.get("wall_clock_ms") or 0) / 1000 or None,
                "gates": r.gates, "observe": r.observe,
                "discarded": [p for p in _field(r, "discarded") or [] if isinstance(p, str)],
                "discarded_frozen": [p for p in _field(r, "discarded_frozen") or [] if isinstance(p, str)],
            })
        return rows

    def state(self) -> dict:
        self._refresh_current()
        config, config_path = self._config()
        records = self.records() if self.paths.run_id else []
        total_cost = summarize_costs([usage for row in records for usage in row.cost_usages])
        iterations = self.iterations(records)
        return {
            "title": (config.title or None) if config and isinstance(config.goal, str) else None,
            "project": self.project.name,
            "goal": config.goal if config else None,
            "directions": config.metric_directions() if config else {},
            "status": read_run_status(self.project, self.paths.run_id),
            "run_id": self.paths.run_id,
            "config": self._config_summary(config, config_path),
            "notes_files": sorted(self._notes_files(), key=_note_order) if self.paths.run_id else [],
            "iterations": iterations,
            "cost": {
                "cost_usd": total_cost.cost_usd,
                "incomplete": total_cost.incomplete,
            },
            "report": self._report(),
        }

    def _report(self) -> dict | None:
        if not self.paths.run_id:
            return None
        html = _markdown(_read_text_at(self.root, ["output", "report.md"]))
        return {"html": html, **(read_report_info(self.project, self.paths.run_id) or {})} if html else None

    def changes(self, n: int) -> str | None:
        """Changes vs baseline: an accepted iteration's code against the run's start, or a
        running one's edits so far. Runs git; the server calls it outside its lock."""
        if not self.paths.run_id:
            return None
        record = next((r for r in self.records() if r.iteration == n), None)
        base = _object(self.paths.read_identity().get("base_commit"))
        if not base:
            return None
        if record is None:
            return self._live_diff(n, base)
        if record.accepted and _object(record.commit):
            return self._diff(base, record.commit)
        return None

    def iteration(self, n: int, logs: bool = True, diff: bool = True, outputs: bool = True) -> dict:
        """One iteration's card. Without `outputs` (report.html), observe entries keep their
        status and review text but not their stdout, stderr or captured files."""
        self._refresh_current()
        if not self.paths.run_id:
            return {"iteration": n, "failure_output": None, "notes": None, "diff": None, "agent": [],
                    "calls": [], "agent_clipped": False, "gates": [], "observe": [], "summary": None,
                    "discarded": [], "attempt": None}
        records = self.records()
        record = next((r for r in records if r.iteration == n), None)
        changes = self.changes(n) if diff else None
        # While an iteration runs, its own notes live in the worktree; a copy in notes/
        # may be left over from an earlier, failed attempt at the same iteration. Once it
        # finished, the version saved with it (the last attempt's: `notes.N-k` after a
        # retry) shows the note as of N, even if the agent later edited or removed it.
        # Each is (folder, path below it), read without following symlinks or blocking on FIFOs.
        note = f"iter-{n}.md"
        latest = [(self.root, [folder, note])] if (folder := self._notes_folder()) else []
        if record is None:
            notes = [(self.paths.copies, [self._agent_copy(n).name, *self._prefix_parts(),
                                          ITERATION_DIR, "notes", note])] + latest
        else:
            names = [name for name, info in _listing(self.root, []) if stat.S_ISDIR(info.st_mode)
                     and re.fullmatch(rf"notes\.{n}(-\d+)?", name)]
            names.sort(key=lambda name: int(name.rsplit("-", 1)[1]) if "-" in name else 0)
            notes = [(self.root, [name, note]) for name in reversed(names)] + latest
        log_dir = self.paths.logs / f"iter-{n}"
        calls = record.calls if record else []
        if record is None:
            from autohelix.usage_log import read_usage

            status = read_run_status(self.project, self.paths.run_id) or {}
            calls = list(status.get("calls", [])) if status.get("iteration") == n else []
            for usage in read_usage(self.paths.runtime / "usage" / f"iter-{n}.jsonl"):
                calls.append({"role": "command", "log": usage.get("log"), "usage": usage})
        phases = []
        for call in calls:
            log = self.paths.relative_log(call.get("log"))
            phases.append({"role": call["role"], "usage": call.get("usage", {}), "log": log,
                           "events": self._events(Path(log)) if logs and log else []})
        agent_events = [event for call in phases if call["role"] == "agent" for event in call["events"]]
        config = self._config()[0]
        if record is not None:
            gates = record.gates
        else:  # still running: nothing is recorded until it ends
            gates = [{"text": gate.text, "kind": "metric" if isinstance(gate, MetricGate) else "command",
                      "passed": None} for gate in (config.gates if config else [])]
        summary = _field(record, "summary")
        base, tree = _object(_field(record, "base")), _object(_field(record, "tree"))
        rejected_ref = _field(record, "rejected_ref")
        return {
            "iteration": n,
            "failure_output": record.failure_output if record else None,
            "notes": _markdown(next(filter(None, (_read_text_at(root, parts) for root, parts in notes)), None)),
            "diff": changes,
            "agent": agent_events,
            "calls": phases,
            "agent_clipped": any(self.paths.run_dir / call["log"] in self._clipped
                                 for call in phases if call.get("log")),
            "gates": gates,
            "observe": self.observations(n, record, config, outputs=outputs),
            "summary": summary if isinstance(summary, str) and summary.strip() else None,
            "discarded": [p for p in _field(record, "discarded") or [] if isinstance(p, str)],
            "discarded_frozen": [p for p in _field(record, "discarded_frozen") or [] if isinstance(p, str)],
            # What `/iteration/<n>/diff` compares: the commit the attempt started from and its frozen tree.
            "attempt": {"base": base, "tree": tree, "commit": record.commit if record else None,
                        "rejected_ref": rejected_ref if isinstance(rejected_ref, str) else None}
                       if base and tree else None,
        }

    def observations(self, n: int, record, config, outputs: bool = True) -> list[dict]:
        """One section per observe entry: those the record lists (in the order they ran),
        then folders in observations/iter-N/ it doesn't (a running iteration's, in config
        order), then any files directly in that folder (the layout before named entries).

        Review entries (the stored config's `review:` entries; without a readable config,
        entries whose folder holds only stdout.txt) get their stdout rendered as Markdown,
        minus the metric lines.

        While the iteration runs (no record yet), entries with a folder have finished
        (their status comes with the record); the one the run status names as running
        (`entry`, in the benchmark phase) is added, marked `running`.

        Everything is read through descriptors from the run folder down, so a symlinked
        observations/, iter-N/ or entry folder shows nothing."""
        where = ["observations", f"iter-{n}"]
        order = [entry.name for entry in config.observe] if config else []
        reviews = {entry.name for entry in config.observe if entry.review is not None} if config else None
        recorded = {}
        for item in (record.observe if record else []):
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                recorded.setdefault(item["name"], item)
        listing = _listing(self.root, where)
        on_disk = {name for name, info in listing if stat.S_ISDIR(info.st_mode) and not name.startswith(".")}
        rank = {name: index for index, name in enumerate(order)}
        names = list(recorded) + sorted(on_disk - set(recorded), key=lambda d: (rank.get(d, len(rank)), d))
        running = None
        if record is None:
            status = read_run_status(self.project, self.paths.run_id) or {}
            if (status.get("state") == "running" and status.get("iteration") == n
                    and status.get("phase") == "benchmark" and isinstance(status.get("entry"), str)):
                running = status["entry"]
                if running not in names:
                    names.append(running)
        sections = []
        for name in names:
            item = recorded.get(name, {})
            here = name in on_disk
            stdout = _read_text_at(self.root, [*where, name, "stdout.txt"], single_link=True) if here else None
            stderr = _read_text_at(self.root, [*where, name, "stderr.txt"], single_link=True) if here else None
            files = _tree_files(self.root, [*where, name], skip=OUTPUT_FILES) if here else []
            review = name in reviews if reviews is not None else bool(stdout) and not stderr and not files
            sections.append({
                "name": name, "review": review, "ran": name in recorded,
                "running": name == running and name not in recorded,
                "ok": item.get("ok"), "error": item.get("error"), "return_code": item.get("return_code"),
                "duration_s": item.get("duration_s"), "metrics": item.get("metrics") or {},
                "stdout": stdout if outputs else None,
                "stdout_file": f"{name}/stdout.txt" if here and stdout is not None else None,
                "stderr": stderr if outputs else None,
                "html": review_markdown(stdout) if review else None,
                "files": [{**f, "path": f"{name}/{f['path']}"} for f in files] if outputs else [],
            })
        legacy = [{"path": name, "size": info.st_size} for name, info in listing
                  if stat.S_ISREG(info.st_mode) and info.st_nlink == 1]
        if legacy:
            sections.append({"name": None, "legacy": True, "review": False, "ran": False, "running": False,
                             "ok": None, "error": None, "return_code": None, "duration_s": None, "metrics": {},
                             "stdout": None, "stdout_file": None, "stderr": None, "html": None,
                             "files": legacy if outputs else []})
        return sections

    def attempt_diff(self, n: int) -> dict:
        """`/iteration/<n>/diff`: the attempt's own change, from the commit it started from
        to its frozen tree (kept by the run ref, or `rejected_ref` for a rejected one).
        Runs git; the server calls it outside its lock."""
        record = next((r for r in self.records() if r.iteration == n), None) if self.paths.run_id else None
        if record is None:
            return {"diff": None, "error": "This iteration has no record yet."}
        base, tree = _object(_field(record, "base")), _object(_field(record, "tree"))
        if not (base and tree):
            return {"diff": None, "error": "This iteration's record names no starting commit and tree."}
        diff, error = self._cached_diff(base, tree)
        if error is not None:
            try:
                missing = [what for what, name in (("starting commit", base), ("tree", tree))
                           if self._repo().git("cat-file", "-e", name, check=False, timeout=5).returncode]
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                missing = []
            if missing:
                error = (f"The attempt's {' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} no longer "
                         "in the repository (its run or rejected ref was deleted).")
            else:
                error = f"Could not read the diff: {error}"
        return {"diff": diff, "error": error}

    def _cached_diff(self, a: str, b: str) -> tuple[str | None, str | None]:
        """`_run_diff(a, b)` for two object names, the last DIFF_CACHE kept (objects
        don't change; a failure is not kept)."""
        key = f"{a}:{b}"
        with self._diff_lock:
            if key in self._diffs:
                self._diffs.move_to_end(key)
                return self._diffs[key]
        found = self._run_diff([a, b, "--"])
        if found[1] is None:
            with self._diff_lock:
                self._diffs[key] = found
                while len(self._diffs) > DIFF_CACHE:
                    self._diffs.popitem(last=False)
        return found

    def _run_diff(self, args: list[str], *, work_tree: Path | None = None, index: Path | None = None,
                  timeout: float = 10) -> tuple[str | None, str | None]:
        """`git diff` on the user's repository with fixed output settings: (text, None),
        cut after MAX_TEXT bytes with a note (git is stopped there), or (None, error)."""
        from autohelix.workspace import _SAFE, git_env

        try:
            repo = self._repo()
            proc = subprocess.Popen(
                ["git", "--git-dir", str(repo.git_dir), *(["--work-tree", str(work_tree)] if work_tree else []),
                 *_SAFE, *DIFF_CONFIG, *DIFF_ARGS, *args],
                cwd=work_tree or repo.root, env=git_env({"GIT_INDEX_FILE": str(index)} if index else None),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        except (OSError, RuntimeError) as exc:
            return None, str(exc)
        expired = threading.Event()

        def stop():
            expired.set()
            proc.kill()

        timer = threading.Timer(timeout, stop)
        timer.start()
        try:
            data = proc.stdout.read(MAX_TEXT + 1)
            cut = len(data) > MAX_TEXT
            if cut:
                proc.kill()
            error = b"" if cut else proc.stderr.read(64 * 1024)
            code = proc.wait()
        finally:
            timer.cancel()
            proc.stdout.close()
            proc.stderr.close()
        if cut:
            return data[:MAX_TEXT].decode(errors="replace") + "\n" + DIFF_CUT.format(MAX_TEXT // 1024) + "\n", None
        if expired.is_set():
            return None, f"git diff took longer than {timeout:g}s"
        if code:
            return None, os.fsdecode(error).strip() or f"git diff exited {code}"
        return data.decode(errors="replace"), None

    def snapshot(self) -> dict:
        """The page's data keyed by route, for report.html: no logs or captured output,
        and one diff, baseline to final code, on the last accepted iteration."""
        state = self.state()
        accepted = [r["iteration"] for r in state["iterations"] if r["iteration"] > 0 and r["accepted"]]
        final = accepted[-1] if accepted else None
        routes = {"/api/state": state}
        for r in state["iterations"]:
            n = r["iteration"]
            routes[f"/api/iteration/{n}"] = self.iteration(n, logs=False, diff=n == final, outputs=False)
        for name in state["notes_files"]:
            # The page builds this URL with encodeURIComponent, which leaves !'()* as is.
            routes["/api/notes/" + quote(name, safe="!'()*")] = self.note(name)
        return routes

    def _repo(self):
        from autohelix.workspace import Repo

        if self._repository is None:
            self._repository = Repo(self.project)
        return self._repository

    def _agent_copy(self, n: int) -> Path:
        return self.paths.copies / f"iter-{n}-agent"

    def _prefix_parts(self) -> list[str]:
        """The project's folder inside the repository, as path components."""
        try:
            prefix = self._repo().prefix
        except RuntimeError:
            prefix = ""
        return [part for part in prefix.split("/") if part]

    def _notes_folder(self) -> str | None:
        """The folder `runs/<id>/notes` stands for: the version its link names (a sibling
        `notes.N`), or a real `notes/` saved before notes had versions."""
        link = self.root / "notes"
        try:
            target = os.readlink(link)
        except OSError:
            return "notes" if link.is_dir() and not link.is_symlink() else None
        return target if re.fullmatch(r"notes\.[^/\0]+", target) else None

    def _notes_files(self) -> list[str]:
        folder = self._notes_folder()
        return [name for name, info in _listing(self.root, [folder]) if stat.S_ISREG(info.st_mode)] if folder else []

    def note(self, name: str) -> dict | None:
        if not self.paths.run_id or name not in self._notes_files():
            return None
        return {"name": name, "html": _markdown(_read_text_at(self.root, [self._notes_folder(), name]))}

    def artifact(self, n: int, parts: list[str]) -> tuple[bytes, str] | None:
        """A file in observations/iter-N/, by its path there (`bench/results/log.json`):
        a regular file with no other hard links (captures are copies)."""
        if not self.paths.run_id:
            return None
        data = _read_bytes(self.root, ["observations", f"iter-{n}", *parts], MAX_ARTIFACT, single_link=True)
        if data is None:
            return None
        # Anything that is not a plain image is served as text, so captured
        # HTML/SVG/JS can never run in the viewer's origin.
        mime = IMAGE_TYPES.get(Path(parts[-1]).suffix.lower(), "text/plain; charset=utf-8")
        return data, mime

    def _events(self, path: Path) -> list[dict]:
        """Incrementally parse a log, merging streamed text into one event."""
        relative = self.paths.relative_log(path)
        if not relative:
            return []
        path = self.paths.run_dir / relative
        if not path.is_file():
            return []
        if path not in self._logs:
            self._logs[path] = (LogTail(path), deque(maxlen=MAX_EVENTS))
        tail, events = self._logs[path]
        try:
            new = tail.read()
        except OSError:
            return list(events)
        if tail.reset:
            events.clear()
            self._clipped.discard(path)
        for event in new:
            if event.kind in (AgentEventKind.RAW, AgentEventKind.STATUS, AgentEventKind.RESULT):
                continue  # Unparsed lines and lifecycle/usage bookkeeping are not shown.
            prose = event.kind in (AgentEventKind.ASSISTANT_TEXT, AgentEventKind.THINKING)
            item = {"kind": str(event.kind), "text": event.text[:20_000 if prose else 4_000],
                    "error": bool(event.metadata.get("is_error"))}
            last = events[-1] if events else None
            if last and last.get("streaming") and last["kind"] == item["kind"] and (
                    event.metadata.get("stream") or event.metadata.get("final")):
                last["text"] = item["text"]  # Streamed events carry accumulated text.
                last["streaming"] = bool(event.metadata.get("stream"))
                item = last
            else:
                item["streaming"] = bool(event.metadata.get("stream"))
                if len(events) == events.maxlen:
                    self._clipped.add(path)
                events.append(item)
            if item["kind"] == AgentEventKind.ASSISTANT_TEXT and not item["streaming"]:
                item["html"] = _markdown(item["text"])  # Render once, when complete.
        return list(events)

    def _subjects(self, commits: list[str]) -> dict[str, str | None]:
        """Commit subjects are the agent's one-line summary of accepted work."""
        missing = [c for c in commits if c not in self._subject_cache and re.fullmatch(r"[0-9a-f]{7,64}", c)]
        if missing:
            try:
                out = subprocess.run(["git", "log", "--no-walk", "--format=%H%x09%s", *missing],
                                     cwd=self.project, capture_output=True, text=True, timeout=5).stdout
            except (OSError, subprocess.TimeoutExpired):
                out = ""
            found = dict(line.split("\t", 1) for line in out.splitlines() if "\t" in line)
            for commit in missing:
                subject = next((v for k, v in found.items() if k.startswith(commit)), None)
                self._subject_cache[commit] = None if not subject or subject.startswith("AutoHelix iteration") else subject
        return {c: self._subject_cache.get(c) for c in commits}

    def _live_diff(self, n: int, base: str) -> str | None:
        """A running iteration's edits so far: its copy compared with the baseline, or
        the frozen candidate once the agent's copy is gone.

        Git runs from outside the copy, through the user's repository, with an
        index of the viewer's own (kept per copy so unchanged files aren't rehashed;
        one request at a time uses it)."""
        if not _object(base):
            return None
        copy = self._agent_copy(n)
        with self._live_lock:
            index = None
            try:
                repo = self._repo()
                if not copy.is_dir() or copy.is_symlink():
                    # The pin holds an earlier iteration's tree until this one is frozen.
                    status = read_run_status(self.project, self.paths.run_id) or {}
                    if status.get("iteration") != n or status.get("phase") not in FROZEN_PHASES:
                        return None
                    diff, error = self._run_diff([base, self.paths.pin, "--"])
                else:
                    from autohelix.workspace import _private_index_dir

                    run, folder = hashlib.sha256(os.fsencode(copy.parent)).hexdigest()[:16], _private_index_dir()
                    index = folder / f"view-{run}-{copy.name}.index"
                    if not index.exists():
                        for old in folder.glob(f"view-{run}-*.index"):
                            old.unlink(missing_ok=True)  # an earlier iteration's copy
                        repo.git("read-tree", base, index=index, timeout=5)
                    repo.git("add", "-A", "--", ".", *repo.unfrozen_excludes(copy, index, timeout=5),
                             work_tree=copy, index=index, timeout=5)
                    diff, error = self._run_diff(["--cached", base, "--"], work_tree=copy, index=index)
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                diff, error = None, "failed"
            if error is not None and index is not None:
                # A killed git can leave its lock behind; start this copy's index afresh next time.
                for stale in (index, index.with_name(index.name + ".lock")):
                    stale.unlink(missing_ok=True)
        return diff or None

    def _diff(self, base: str, commit: str) -> str | None:
        """Everything changed between the baseline and this iteration's accepted code."""
        if not (_object(base) and _object(commit)):
            return None
        diff, error = self._cached_diff(base, commit)
        return diff if error is None else f"Could not read the diff: {error}"


RUN_ID = r"[a-z0-9][a-z0-9-]{0,50}"
# `dashboard --json` / `view --json` (and /api/runs): fields are only ever added.
JSON_SCHEMA = 1


class ProjectRuns:
    """Reads every run in a project's .autohelix/runs/ for the dashboard."""

    def __init__(self, project: Path):
        self.project = project.resolve()
        self._histories: dict[str, History] = {}
        self._configs: dict[Path, tuple[float, dict]] = {}
        self._counts: dict[tuple[str, str], int | None] = {}

    def exists(self, run_id: str) -> bool:
        return bool(re.fullmatch(RUN_ID, run_id)) and (RunPaths(self.project).runs / run_id / "run.json").is_file()

    def runs(self) -> dict:
        """The run list: `/api/runs` and `dashboard --json` (add-only, see JSON_SCHEMA)."""
        root = RunPaths(self.project)
        refs = self._refs()
        rows = []
        for identity in root.runs.glob("*/run.json"):
            run_id = identity.parent.name
            if not re.fullmatch(RUN_ID, run_id):
                continue
            try:
                rows.append(self._run(run_id, refs))
            except Exception as exc:  # one unreadable run must not hide the others
                rows.append({"id": run_id, "error": str(exc)})
        rows.sort(key=lambda r: r.get("last_activity") or 0, reverse=True)
        return {**self._header(), "runs": rows}

    def run(self, run_id: str) -> dict:
        """One run for `view --json`: its list row, plus the goal and every iteration."""
        row = self._run(run_id, self._refs())
        data = RunData(self.project, run_id)
        return {**self._header(), "run": {**row, "iterations": data.iterations(data.records())}}

    def _header(self) -> dict:
        # What the checkout is on: the reference point for each run's landing state.
        branch = git(self.project, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip() or None
        commit = git(self.project, "rev-parse", "--short", "HEAD", check=False).stdout.strip() or None
        home = Path.home()
        display = f"~/{self.project.relative_to(home)}" if self.project.is_relative_to(home) else str(self.project)
        return {"schema": JSON_SCHEMA, "project": self.project.name, "host": socket.gethostname().split(".")[0],
                "path": str(self.project), "path_display": display, "branch": branch, "commit": commit}

    def _refs(self) -> dict[str, str]:
        try:
            out = git(self.project, "for-each-ref", "--format=%(refname)%09%(objectname)",
                      "refs/autohelix", "refs/heads").stdout
        except (OSError, RuntimeError):
            return {}
        return dict(line.split("\t", 1) for line in out.splitlines() if "\t" in line)

    def _config(self, relative: str | None) -> dict:
        if not relative:
            return {}
        path = Path(relative) if Path(relative).is_absolute() else self.project / relative
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {"file": relative, "missing": True}
        cached = self._configs.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            config = load_config(self.project, config_file=path)[0]
            summary = {"file": relative, "goal": (config.goal.strip() or None) if isinstance(config.goal, str) else None,
                       "title": (config.title or None) if isinstance(config.goal, str) else None,
                       "directions": config.metric_directions(),
                       "max_iterations": config.max_iterations, "max_cost_usd": config.max_cost_usd,
                       "max_time_seconds": config.max_time_seconds, "land": config.land}
        except Exception:
            summary = {"file": relative, "invalid": True}
        self._configs[path] = (mtime, summary)
        return summary

    def _landing(self, target_tip: str | None, tip: str | None) -> dict | None:
        """Commits on the run that its target lacks, and whether a fast-forward could add them."""
        if not (target_tip and tip and COMMIT.fullmatch(target_tip) and COMMIT.fullmatch(tip)):
            return None
        key = (target_tip, tip)
        if key not in self._counts:
            count = git(self.project, "rev-list", "--count", f"{target_tip}..{tip}", check=False)
            ancestor = git(self.project, "merge-base", "--is-ancestor", target_tip, tip, check=False)
            self._counts[key] = None if count.returncode or not count.stdout.strip().isdigit() else {
                "commits": int(count.stdout), "fast_forward": ancestor.returncode == 0}
        return self._counts[key]

    @staticmethod
    def _metrics(records, directions: dict[str, str]) -> dict | None:
        """The latest accepted measurement (the code a land would deliver) against the baseline.

        Not the best value per metric: acceptance does not require improvement, and
        per-metric bests can come from different iterations."""
        latest = next((r for r in reversed(records) if r.accepted and r.metrics), None)
        if latest is None:
            return None
        baseline = next((r.metrics for r in records if r.iteration == 0), {}) or {}
        names = [n for n in directions if n in latest.metrics] + [n for n in latest.metrics if n not in directions]
        return {"iteration": latest.iteration, "values": [
            {"name": n, "value": latest.metrics[n], "baseline": baseline.get(n), "direction": directions.get(n)}
            for n in names]}

    def _run(self, run_id: str, refs: dict[str, str]) -> dict:
        paths = RunPaths(self.project, run_id)
        data = paths.read_identity()
        status = read_run_status(self.project, run_id)
        history = self._histories.setdefault(run_id, History(self.project, run_id))
        records = history.load()
        # Read from the checkout's current file (phase 2: the segment's config snapshot).
        config = self._config(data.get("config_path"))

        attempts = [r for r in records if r.iteration > 0]
        cost = summarize_costs([usage for r in records for usage in r.cost_usages])
        tip = refs.get(paths.ref) or data.get("abandoned_tip")
        target = data.get("target_branch")
        target_tip = refs.get(f"refs/heads/{target}") if target else None
        return {
            "id": run_id,
            "name": data.get("name"),
            "archived": is_archived(data),
            "created_at": data.get("created_at"),
            "last_activity": last_activity(paths, data, status or {}),
            "config_file": config.get("file"),
            # The run's own file, gone (never replaced by another) or no longer valid.
            "config_missing": bool(config.get("missing")),
            "config_invalid": bool(config.get("invalid")),
            "goal": config.get("goal"),
            "title": config.get("title"),  # the goal's first line, cut (Config.title)
            "status": status and {k: status.get(k) for k in (
                "state", "phase", "iteration", "iteration_started_at", "phase_started_at", "stopped_at",
                "stop_reason", "max_iterations")},
            "accepted": sum(1 for r in attempts if r.accepted),
            "metrics": self._metrics(records, config.get("directions") or {}),
            # Budgets are run totals across segments; `run -n` overrides the file for a segment.
            "progress": {
                "iterations": max((r.iteration for r in attempts), default=0),
                "max_iterations": (status or {}).get("max_iterations") or config.get("max_iterations"),
                "cost_usd": cost.cost_usd, "cost_incomplete": cost.incomplete,
                "max_cost_usd": config.get("max_cost_usd"),
                # As the time budget counts it: iteration wall-clock, not the baseline.
                "seconds": sum(r.usage.get("wall_clock_ms", r.usage.get("duration_ms", 0)) or 0
                               for r in attempts) / 1000,
                "max_seconds": config.get("max_time_seconds"),
            },
            "target_branch": target,
            "landing": self._landing(target_tip, tip),
            "group": data.get("group"),
            # Recorded at creation; older runs follow the config (else auto).
            "land": data.get("land") or config.get("land") or "auto",
        }


def write_static(project: Path, *, run_id: str | None = None) -> Path:
    """Write output/report.html: the viewer page with this run's data built in."""
    resources = files("autohelix").joinpath("viewer_resources")
    read = lambda name: resources.joinpath(name).read_text(encoding="utf-8")  # noqa: E731
    # Each logo once, in the data; the page's theme code sets the <img> from it.
    logos = {theme: "data:image/svg+xml," + quote(read(f"autohelix-{theme}.svg"), safe=" =:/-.,;'\"()")
             for theme in ("light", "dark")}
    data = {"routes": RunData(project, run_id).snapshot(), "logos": logos, "saved_at": time.time()}
    # "<" escaped, so no text in the data can close the script element.
    script = json.dumps(data).replace("<", "\\u003c")
    page = (read("index.html")
            # No requests at all: images in hostile Markdown can't call out; only inline code and data: images.
            .replace('<meta charset="utf-8">',
                     f'<meta charset="utf-8">\n  <meta http-equiv="Content-Security-Policy" content="{STATIC_CSP}">')
            .replace('<link rel="stylesheet" href="/viewer.css">', f"<style>\n{read('viewer.css')}</style>")
            .replace('<script src="/viewer.js" defer></script>', "")
            .replace('  <link rel="icon" href="/logo-light.svg">\n', "")
            .replace(' src="/logo-light.svg"', "")
            .replace("</body>", f"<script>window.AUTOHELIX_SNAPSHOT = {script};</script>\n"
                                f"<script>\n{read('viewer.js')}</script>\n</body>"))
    out = RunPaths(project, run_id).output / "report.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(page, encoding="utf-8")
    tmp.replace(out)
    return out


def make_server(project: Path, port: int = 8765, *, run_id: str | None = None) -> ThreadingHTTPServer:
    data = RunData(project, run_id)
    dashboard = ProjectRuns(project)
    others: dict[str, RunData] = {}
    # RunData's caches and log tails are not thread-safe. Git diffs run outside the lock
    # (RunData guards their cache itself), so one big diff doesn't stall every page.
    lock = threading.Lock()
    resources = files("autohelix").joinpath("viewer_resources")

    def run_data(rid: str) -> RunData | None:
        if not dashboard.exists(rid):
            return None
        with lock:
            if rid == data.paths.run_id:
                return data  # the default run's, already reading
            if rid not in others:
                others[rid] = RunData(project, rid)
            return others[rid]

    def run_route(rd: RunData, path: str) -> tuple | None:
        if path == "/state":
            with lock:
                return 200, rd.state()
        if match := re.fullmatch(r"/iteration/(\d{1,9})", path):
            with lock:
                detail = rd.iteration(int(match[1]), diff=False)
            return 200, {**detail, "diff": rd.changes(int(match[1]))}
        if match := re.fullmatch(r"/iteration/(\d{1,9})/diff", path):
            return 200, rd.attempt_diff(int(match[1]))
        if match := re.fullmatch(r"/notes/([^/]+)", path):
            if note := rd.note(unquote(match[1])):
                return 200, note
        # Each path component is decoded on its own, so an encoded "/" can't add one.
        if match := re.fullmatch(r"/artifact/(\d{1,9})/(.+)", path):
            if found := rd.artifact(int(match[1]), [unquote(part) for part in match[2].split("/")]):
                return 200, *found
        return None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status: int, body, mime="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            # Only answer to loopback host names (blocks DNS-rebinding pages).
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host not in LOOPBACK:
                return self._send(403, {"error": "forbidden host"})
            try:
                response = self._route(urlsplit(self.path).path)
                self._send(*response)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:  # A half-written file should not kill the page.
                self._send(500, {"error": str(exc)})

        def _route(self, path: str) -> tuple:
            if path in ASSETS:
                name, mime = ASSETS[path]
                return 200, resources.joinpath(name).read_bytes(), mime
            if match := re.fullmatch(rf"/run/({RUN_ID})/", path):
                if dashboard.exists(match[1]):
                    return 200, resources.joinpath("index.html").read_bytes(), "text/html; charset=utf-8"
            elif path == "/api/runs":
                with lock:
                    return 200, dashboard.runs()
            elif match := re.fullmatch(rf"/api/run/({RUN_ID})(/.+)", path):
                if (rd := run_data(match[1])) and (response := run_route(rd, match[2])):
                    return response
            elif path.startswith("/api/") and (response := run_route(data, path[4:])):
                return response
            return 404, {"error": "not found"}

    # Threads, so one idle browser connection (e.g. a preconnect) cannot stall the page.
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server
