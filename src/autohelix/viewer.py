# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""`autohelix view --web` / `autohelix dashboard --web`: a small live web app, served on 127.0.0.1.

Two pages share one server: `/` lists the project's runs, `/run/<id>/` shows one
run. The pages poll a few JSON routes. There are no write routes. Every file read
is derived from a run id that must name an existing run directory, an iteration
number, a file name that must match an entry in notes/, or a relative path inside
one iteration's observations/iter-N/ folder, opened one component at a time
without following symlinks (`_open_inside`). Git only gets object names from
history records that are plain hex. The unprefixed `/api/...` routes serve the
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
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from markdown_it import MarkdownIt

from autohelix.agents import AgentEventKind
from autohelix.config import MetricGate, load_config
from autohelix.costs import summarize_costs
from autohelix.history import History
from autohelix.runs import RunPaths, git, is_archived, last_activity
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


def _open_inside(folder: Path, parts: list[str]) -> int | None:
    """A read descriptor for the regular file `folder/parts...`, or None.

    `folder` and every component below it are opened without following symlinks,
    each relative to the one before: no `..`, no absolute paths, nothing outside
    `folder`, and no FIFOs or devices."""
    if not parts or any(part in ("", ".", "..") or "/" in part or "\0" in part for part in parts):
        return None
    fds: list[int] = []
    try:
        fds.append(os.open(folder, _OPEN | os.O_DIRECTORY))
        for part in parts[:-1]:
            fds.append(os.open(part, _OPEN | os.O_DIRECTORY, dir_fd=fds[-1]))
        fd = os.open(parts[-1], _OPEN | os.O_NONBLOCK, dir_fd=fds[-1])
    except OSError:
        return None
    finally:
        for parent in fds:
            os.close(parent)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return None
    return fd


def _read_inside(folder: Path, parts: list[str], limit: int) -> bytes | None:
    """The whole file, through `_open_inside`; None if it is larger than `limit`."""
    fd = _open_inside(folder, parts)
    if fd is None:
        return None
    with os.fdopen(fd, "rb") as f:
        return None if os.fstat(f.fileno()).st_size > limit else f.read()


def _read_text_inside(folder: Path, parts: list[str]) -> str | None:
    """`_read_text` through `_open_inside`."""
    fd = _open_inside(folder, parts)
    if fd is None:
        return None
    with os.fdopen(fd, "rb") as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - MAX_TEXT))
        text = f.read().decode("utf-8", errors="replace")
    return text if size <= MAX_TEXT else "[… earlier content omitted …]\n" + text


def _tree_files(folder: Path, skip: tuple[str, ...] = ()) -> list[dict]:
    """Regular files below a folder as `{"path": "a/b.txt", "size"}`, at most MAX_FILES;
    symlinks (to files or folders) are left out, and so are `skip` names at the top."""
    out: list[dict] = []
    for top, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(top, d)))
        for name in sorted(names):
            if top == str(folder) and name in skip:
                continue
            try:
                info = os.lstat(os.path.join(top, name))
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                out.append({"path": (Path(top) / name).relative_to(folder).as_posix(), "size": info.st_size})
                if len(out) >= MAX_FILES:
                    return out
    return out


def _read_text(path: Path) -> str | None:
    """Read a text file, keeping the tail if it is very large."""
    try:
        with path.open("rb") as f:
            size = f.seek(0, 2)
            f.seek(max(0, size - MAX_TEXT))
            text = f.read().decode("utf-8", errors="replace")
        return text if size <= MAX_TEXT else "[… earlier content omitted …]\n" + text
    except OSError:
        return None


def _files(directory: Path) -> list[os.DirEntry]:
    """Regular files directly in a directory (no symlinks, no subdirectories)."""
    try:
        return [e for e in os.scandir(directory) if e.is_file(follow_symlinks=False)]
    except OSError:
        return []


def _note_order(name: str):
    match = re.fullmatch(r"iter-(\d+)\.md", name)
    return (0, int(match[1]), "") if match else (1, 0, name)


class RunData:
    """Reads one project's .autohelix/ directory for the viewer."""

    def __init__(self, project: Path, config_file: Path | None = None, run_id: str | None = None):
        self.project = project.resolve()
        self.run_id = run_id
        self.paths = RunPaths(self.project, run_id)
        self.root = self.paths.run_dir
        self.config_file = config_file
        self.history = History(self.project, self.paths.run_id)
        self._logs: dict[Path, tuple[LogTail, deque]] = {}
        self._diffs: dict[str, str] = {}
        self._clipped: set[Path] = set()  # logs whose oldest events were dropped
        self._subject_cache: dict[str, str | None] = {}
        self._repository = None

    def _refresh_current(self):
        if self.run_id in (None, "current"):
            paths = RunPaths(self.project)
            if paths.run_id != self.paths.run_id:
                self.paths = paths
                self.root = paths.run_dir
                self.history = History(self.project, paths.run_id)
                self._logs.clear()
                self._diffs.clear()

    def _config(self):
        stored = read_stored_config_path(self.project, self.paths.run_id) if self.paths.run_id else None
        path = self.config_file or stored or self.project / "autohelix.yaml"
        try:
            return load_config(self.project, config_file=path)[0], path
        except Exception:
            return None, path

    @staticmethod
    def _config_summary(config, path: Path) -> dict:
        summary = {"file": path.name, "text": _read_text(path)}
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
            })
        return rows

    def state(self) -> dict:
        self._refresh_current()
        config, config_path = self._config()
        records = self.history.load() if self.paths.run_id else []
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
            "notes_files": sorted((e.name for e in _files(self.root / "notes")), key=_note_order) if self.paths.run_id else [],
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
        html = _markdown(_read_text(self.root / "output" / "report.md"))
        return {"html": html, **(read_report_info(self.project, self.paths.run_id) or {})} if html else None

    def iteration(self, n: int, logs: bool = True, diff: bool = True, outputs: bool = True) -> dict:
        """One iteration's card. Without `outputs` (report.html), observe entries keep their
        status and review text but not their stdout, stderr or captured files."""
        self._refresh_current()
        if not self.paths.run_id:
            return {"iteration": n, "failure_output": None, "notes": None, "diff": None, "agent": [],
                    "calls": [], "agent_clipped": False, "gates": [], "observe": [], "summary": None,
                    "discarded": [], "attempt": None}
        records = self.history.load()
        record = next((r for r in records if r.iteration == n), None)
        # The baseline is the code the run started from: the parent of the first accepted commit.
        first = next((r.commit for r in records if r.iteration > 0 and r.accepted and r.commit), None)
        base = (self.paths.read_identity()["base_commit"] if self.paths.run_id
                else f"{first}^" if first and COMMIT.fullmatch(first) else None)
        changes = None
        if not diff:
            pass
        elif record is None:  # Still running: its edits so far, from the worktree.
            changes = self._live_diff(n, base)
        elif record.accepted and base and record.commit:
            changes = self._diff(base, record.commit)
        # While an iteration runs, its own notes live in the worktree; a copy in notes/
        # may be left over from an earlier, failed attempt at the same iteration. Once it
        # finished, the version saved with it (the last attempt's: `notes.N-k` after a
        # retry) shows the note as of N, even if the agent later edited or removed it.
        latest = self.root / "notes" / f"iter-{n}.md"
        live = self._worktree(n) / ITERATION_DIR / "notes" / f"iter-{n}.md"
        if record is None:
            notes = [live, latest]
        else:
            versions = sorted(self.root.glob(f"notes.{n}"), key=str) + sorted(
                self.root.glob(f"notes.{n}-*"), key=lambda p: int(p.name.rsplit("-", 1)[1])
                if p.name.rsplit("-", 1)[1].isdigit() else 0)
            notes = [v / f"iter-{n}.md" for v in reversed(versions)] + [latest]
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
            "notes": _markdown(next(filter(None, map(_read_text, notes)), None)),
            "diff": changes,
            "agent": agent_events,
            "calls": phases,
            "agent_clipped": any(self.paths.run_dir / call["log"] in self._clipped
                                 for call in phases if call.get("log")),
            "gates": gates,
            "observe": self.observations(n, record, config, outputs=outputs),
            "summary": summary if isinstance(summary, str) and summary.strip() else None,
            "discarded": [p for p in _field(record, "discarded") or [] if isinstance(p, str)],
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
        minus the metric lines."""
        folder = self.root / "observations" / f"iter-{n}"
        order = [entry.name for entry in config.observe] if config else []
        reviews = {entry.name for entry in config.observe if entry.review is not None} if config else None
        recorded = {}
        for item in (record.observe if record else []):
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                recorded.setdefault(item["name"], item)
        try:
            listing = [] if folder.is_symlink() else list(os.scandir(folder))
        except OSError:
            listing = []
        on_disk = {e.name for e in listing if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")}
        rank = {name: index for index, name in enumerate(order)}
        names = list(recorded) + sorted(on_disk - set(recorded), key=lambda d: (rank.get(d, len(rank)), d))
        sections = []
        for name in names:
            item = recorded.get(name, {})
            here = folder / name if name in on_disk else None
            stdout = _read_text_inside(folder, [name, "stdout.txt"]) if here else None
            stderr = _read_text_inside(folder, [name, "stderr.txt"]) if here else None
            files = _tree_files(here, skip=OUTPUT_FILES) if here else []
            review = name in reviews if reviews is not None else bool(stdout) and not stderr and not files
            sections.append({
                "name": name, "review": review, "ran": name in recorded,
                "ok": item.get("ok"), "error": item.get("error"), "return_code": item.get("return_code"),
                "duration_s": item.get("duration_s"), "metrics": item.get("metrics") or {},
                "stdout": stdout if outputs else None,
                "stdout_file": f"{name}/stdout.txt" if here and stdout is not None else None,
                "stderr": stderr if outputs else None,
                "html": review_markdown(stdout) if review else None,
                "files": [{**f, "path": f"{name}/{f['path']}"} for f in files] if outputs else [],
            })
        legacy = sorted(({"path": e.name, "size": e.stat(follow_symlinks=False).st_size}
                         for e in listing if e.is_file(follow_symlinks=False)), key=lambda f: f["path"])
        if legacy:
            sections.append({"name": None, "legacy": True, "review": False, "ran": False, "ok": None,
                             "error": None, "return_code": None, "duration_s": None, "metrics": {},
                             "stdout": None, "stdout_file": None, "stderr": None, "html": None,
                             "files": legacy if outputs else []})
        return sections

    def attempt_diff(self, n: int) -> dict:
        """`/iteration/<n>/diff`: the attempt's own change, from the commit it started from
        to its frozen tree (kept by the run ref, or `rejected_ref` for a rejected one)."""
        self._refresh_current()
        record = next((r for r in self.history.load() if r.iteration == n), None) if self.paths.run_id else None
        if record is None:
            return {"diff": None, "error": "This iteration has no record yet."}
        base, tree = _object(_field(record, "base")), _object(_field(record, "tree"))
        if not (base and tree):
            return {"diff": None, "error": "This iteration's record names no starting commit and tree."}
        key = f"{base}:{tree}"
        if key not in self._diffs:
            try:
                result = self._repo().git("diff", "--no-ext-diff", "--no-textconv", base, tree, "--",
                                          check=False, timeout=10)
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                return {"diff": None, "error": f"Could not read the diff: {exc}"}
            if result.returncode:
                missing = [what for what, name in (("starting commit", base), ("tree", tree))
                           if self._repo().git("cat-file", "-e", name, check=False, timeout=5).returncode]
                return {"diff": None, "error": (
                    f"The attempt's {' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} no longer "
                    "in the repository (its run or rejected ref was deleted)." if missing
                    else "Could not read the diff: " + os.fsdecode(result.stderr).strip())}
            text = result.stdout.decode(errors="replace")
            self._diffs[key] = text[:MAX_TEXT] + ("\n[… diff cut …]\n" if len(text) > MAX_TEXT else "")
        return {"diff": self._diffs[key], "error": None}

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

    def _worktree(self, n: int) -> Path:
        """Where a running iteration's files live (notes are copied back at the end)."""
        try:
            prefix = self._repo().prefix
        except RuntimeError:
            prefix = ""
        return self._agent_copy(n) / prefix

    def note(self, name: str) -> dict | None:
        if not self.paths.run_id:
            return None
        entry = next((e for e in _files(self.root / "notes") if e.name == name), None)
        return {"name": name, "html": _markdown(_read_text(Path(entry.path)))} if entry else None

    def artifact(self, n: int, parts: list[str]) -> tuple[bytes, str] | None:
        """A file in observations/iter-N/, by its path there (`bench/results/log.json`)."""
        if not self.paths.run_id:
            return None
        data = _read_inside(self.root / "observations" / f"iter-{n}", parts, MAX_ARTIFACT)
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
        index of the viewer's own (kept per copy so unchanged files aren't rehashed)."""
        if not COMMIT.fullmatch(base.removesuffix("^")):
            return None
        copy = self._agent_copy(n)
        index = None
        try:
            repo = self._repo()
            if not copy.is_dir() or copy.is_symlink():
                # The pin holds an earlier iteration's tree until this one is frozen.
                status = read_run_status(self.project, self.paths.run_id) or {}
                if status.get("iteration") != n or status.get("phase") not in FROZEN_PHASES:
                    return None
                diff = repo.git("diff", "--no-ext-diff", "--no-textconv", base, self.paths.pin, "--", timeout=5)
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
                diff = repo.git("diff", "--cached", "--no-ext-diff", "--no-textconv", base, "--",
                                work_tree=copy, index=index, timeout=5)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            # A killed git can leave its lock behind; start this copy's index afresh next time.
            for stale in (index, index.with_name(index.name + ".lock")) if index else ():
                stale.unlink(missing_ok=True)
            return None
        return diff.stdout.decode(errors="replace")[:MAX_TEXT] or None

    def _diff(self, base: str, commit: str) -> str:
        """Everything changed between the baseline and this iteration's accepted code."""
        if not (COMMIT.fullmatch(base.removesuffix("^")) and COMMIT.fullmatch(commit)):
            return "Invalid commit reference."
        key = f"{base}..{commit}"
        if key not in self._diffs:
            try:
                result = subprocess.run(
                    ["git", "diff", "--no-ext-diff", "--no-textconv", base, commit, "--"],
                    cwd=self.project, capture_output=True, text=True, errors="replace", timeout=5,
                )
                diff = result.stdout if result.returncode == 0 else result.stderr.strip()
            except (OSError, subprocess.TimeoutExpired) as exc:
                diff = f"Could not read diff: {exc}"
            self._diffs[key] = diff[:MAX_TEXT]
        return self._diffs[key]


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

    def run(self, run_id: str, config_file: Path | None = None) -> dict:
        """One run for `view --json`: its list row, plus the goal and every iteration."""
        row = self._run(run_id, self._refs(), config_file)
        data = RunData(self.project, config_file, run_id)
        return {**self._header(), "run": {**row, "iterations": data.iterations(data.history.load())}}

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

    def _run(self, run_id: str, refs: dict[str, str], config_file: Path | None = None) -> dict:
        paths = RunPaths(self.project, run_id)
        data = paths.read_identity()
        status = read_run_status(self.project, run_id)
        history = self._histories.setdefault(run_id, History(self.project, run_id))
        records = history.load()
        if config_file:  # as typed, from the shell's folder; shown relative to the project when inside it
            config_file = config_file.resolve()
            config_file = config_file.relative_to(self.project) if config_file.is_relative_to(self.project) else config_file
        # Read from the checkout's current file (phase 2: the segment's config snapshot).
        config = self._config(str(config_file) if config_file else data.get("config_path"))

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


def write_static(project: Path, config_file: Path | None = None, *, run_id: str | None = None) -> Path:
    """Write output/report.html: the viewer page with this run's data built in."""
    resources = files("autohelix").joinpath("viewer_resources")
    read = lambda name: resources.joinpath(name).read_text(encoding="utf-8")  # noqa: E731
    # Each logo once, in the data; the page's theme code sets the <img> from it.
    logos = {theme: "data:image/svg+xml," + quote(read(f"autohelix-{theme}.svg"), safe=" =:/-.,;'\"()")
             for theme in ("light", "dark")}
    data = {"routes": RunData(project, config_file, run_id).snapshot(), "logos": logos, "saved_at": time.time()}
    # "<" escaped, so no text in the data can close the script element.
    script = json.dumps(data).replace("<", "\\u003c")
    page = (read("index.html")
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


def make_server(project: Path, port: int = 8765, config_file: Path | None = None, *, run_id: str | None = None) -> ThreadingHTTPServer:
    data = RunData(project, config_file, run_id)
    dashboard = ProjectRuns(project)
    others: dict[str, RunData] = {}
    lock = threading.Lock()  # RunData's caches and log tails are not thread-safe
    resources = files("autohelix").joinpath("viewer_resources")

    def run_data(rid: str) -> RunData | None:
        if not dashboard.exists(rid):
            return None
        if rid == data.paths.run_id:
            return data  # keeps an explicit --config for the default run
        if rid not in others:
            others[rid] = RunData(project, None, rid)
        return others[rid]

    def run_route(rd: RunData, path: str) -> tuple | None:
        if path == "/state":
            return 200, rd.state()
        if match := re.fullmatch(r"/iteration/(\d{1,9})", path):
            return 200, rd.iteration(int(match[1]))
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
                with lock:
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
