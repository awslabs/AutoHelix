# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""`autohelix view`: a small live web view of a run, served on 127.0.0.1.

The page polls a few JSON routes. There are no write routes. Every file read
is derived from an iteration number, or is a file name that must match an
entry in a fixed directory listing (notes/, observations/iter-N/).
"""

import json
import os
import re
import subprocess
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

from autohelix.agents import AgentEventKind
from autohelix.config import load_config
from autohelix.history import History
from autohelix.replay import LogTail
from autohelix.run_status import read_run_status
from autohelix.state import read_stored_config_path

MAX_TEXT = 256 * 1024
MAX_EVENTS = 3000  # merged events kept per log; tool output is clipped to keep this small
MAX_ARTIFACT = 20 * 1024 * 1024
MAX_NEW_FILES = 20  # untracked files shown in a running iteration's diff
COMMIT = re.compile(r"[0-9a-f]{7,64}")
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}
LOOPBACK = {"127.0.0.1", "localhost", "[::1]"}
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/viewer.css": ("viewer.css", "text/css; charset=utf-8"),
    "/viewer.js": ("viewer.js", "text/javascript; charset=utf-8"),
    "/logo-light.svg": ("autohelix-light.svg", "image/svg+xml"),
    "/logo-dark.svg": ("autohelix-dark.svg", "image/svg+xml"),
}
# Raw HTML in notes/reviews is shown as text, never rendered.
_MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
CSP = "default-src 'self'; object-src 'none'; frame-ancestors 'none'"


def _markdown(text: str | None) -> str | None:
    return _MARKDOWN.render(text) if text else None


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

    def __init__(self, project: Path, config_file: Path | None = None):
        self.project = project.resolve()
        self.root = self.project / ".autohelix"
        self.config_file = config_file
        self.history = History(self.project)
        self._logs: dict[Path, tuple[LogTail, deque]] = {}
        self._diffs: dict[str, str] = {}
        self._clipped: set[Path] = set()  # logs whose oldest events were dropped
        self._subject_cache: dict[str, str | None] = {}
        self._prefix: str | None = None

    def _config(self):
        path = self.config_file or read_stored_config_path(self.project) or self.project / "autohelix.yaml"
        try:
            return load_config(self.project, config_file=path)[0], path
        except Exception:
            return None, path

    @staticmethod
    def _config_summary(config, path: Path) -> dict:
        summary = {"file": path.name, "text": _read_text(path)}
        if config:
            summary.update(
                agent=" · ".join(filter(None, [config.agent.type, config.agent.model])),
                checks=[c.command for c in config.constraints],
                scope=config.editable,
                reviewer=config.reviewer is not None,
                max_iterations=config.max_iterations,
                max_cost_usd=config.max_cost_usd,
                max_time_seconds=config.max_time_seconds,
            )
        return summary

    def state(self) -> dict:
        config, config_path = self._config()
        records = self.history.load()
        subjects = self._subjects([r.commit for r in records if r.commit])
        iterations = [
            {"iteration": r.iteration, "accepted": r.accepted, "metrics": r.metrics,
             "summary": r.reason if r.reason != "baseline" else None,
             "cost": r.usage.get("cost_usd"),
             "seconds": (r.usage.get("wall_clock_ms") or 0) / 1000 or None}
            for r in records
        ]
        for item, record in zip(iterations, records):
            item["summary"] = item["summary"] or subjects.get(record.commit)
        return {
            "title": getattr(config, "title", None) or self.project.name,
            "goal": config.goal if config else None,
            "directions": config.metric_directions() if config else {},
            "status": read_run_status(self.project),
            "config": self._config_summary(config, config_path),
            "notes_files": sorted((e.name for e in _files(self.root / "notes")), key=_note_order),
            "artifacts": self._artifacts(),
            "iterations": iterations,
        }

    def iteration(self, n: int) -> dict:
        records = self.history.load()
        record = next((r for r in records if r.iteration == n), None)
        # The baseline is the code the run started from: the parent of the first accepted commit.
        first = next((r.commit for r in records if r.iteration > 0 and r.accepted and r.commit), None)
        base = f"{first}^" if first and COMMIT.fullmatch(first) else None
        diff = None
        if record is None:  # Still running: its edits so far, from the worktree.
            diff = self._live_diff(n, base)
        elif record.accepted and base and record.commit:
            diff = self._diff(base, record.commit)
        # While an iteration runs, its own notes live in the worktree; a copy in notes/
        # may be left over from an earlier, failed attempt at the same iteration.
        notes = [self.root / "notes" / f"iter-{n}.md", self._worktree(n) / ".autohelix" / "notes" / f"iter-{n}.md"]
        if record is None:
            notes.reverse()
        logs = self.root / "logs" / f"iter-{n}"
        return {
            "iteration": n,
            "failure_output": record.failure_output if record else None,
            "notes": _markdown(next(filter(None, map(_read_text, notes)), None)),
            "review": _markdown(_read_text(self.root / "reviews" / f"iter-{n}.md")),
            "diff": diff,
            "agent": self._events(logs / "agent.log"),
            "agent_clipped": logs / "agent.log" in self._clipped,
            "reviewer": self._events(logs / "review.log"),
        }

    def _worktree(self, n: int) -> Path:
        """Where a running iteration's files live (notes are copied back at the end)."""
        if self._prefix is None:
            try:
                self._prefix = subprocess.run(["git", "rev-parse", "--show-prefix"], cwd=self.project,
                                              capture_output=True, text=True, timeout=5).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                self._prefix = ""
        return self.root / "worktrees" / f"iter-{n}" / self._prefix

    def _artifacts(self) -> dict[int, list[dict]]:
        """Captured files per iteration, from observations/iter-N/."""
        out = {}
        try:
            entries = list(os.scandir(self.root / "observations"))
        except OSError:
            return out
        for entry in entries:
            match = re.fullmatch(r"iter-(\d+)", entry.name)
            if match and entry.is_dir(follow_symlinks=False):
                files = sorted(({"name": e.name, "size": e.stat().st_size} for e in _files(Path(entry.path))),
                               key=lambda a: a["name"])
                if files:
                    out[int(match[1])] = files
        return dict(sorted(out.items()))

    def note(self, name: str) -> dict | None:
        entry = next((e for e in _files(self.root / "notes") if e.name == name), None)
        return {"name": name, "html": _markdown(_read_text(Path(entry.path)))} if entry else None

    def artifact(self, n: int, name: str) -> tuple[bytes, str] | None:
        entry = next((e for e in _files(self.root / "observations" / f"iter-{n}") if e.name == name), None)
        if entry is None or entry.stat().st_size > MAX_ARTIFACT:
            return None
        # Anything that is not a plain image is served as text, so captured
        # HTML/SVG/JS can never run in the viewer's origin.
        mime = IMAGE_TYPES.get(Path(name).suffix.lower(), "text/plain; charset=utf-8")
        return Path(entry.path).read_bytes(), mime

    def _events(self, path: Path) -> list[dict]:
        """Incrementally parse a log, merging streamed text into one event."""
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

    def _live_diff(self, n: int, base: str | None) -> str | None:
        """A running iteration's edits so far: its worktree compared with the baseline."""
        worktree = self._worktree(n)
        if not worktree.is_dir():
            return None

        def git(*args, cwd=worktree):
            return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                                  errors="replace", timeout=5)

        try:
            if base is None:  # No accepted iteration yet: the project's HEAD is the baseline.
                base = git("rev-parse", "HEAD", cwd=self.project).stdout.strip()
            if not COMMIT.fullmatch(base.removesuffix("^")):
                return None
            diff = git("diff", "--no-ext-diff", "--no-textconv", base, "--").stdout
            # New files are untracked in the worktree, so plain `git diff` skips them.
            new = git("ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0")
            for name in [f for f in new if f][:MAX_NEW_FILES]:
                diff += git("diff", "--no-index", "--no-ext-diff", "--no-textconv", "--", os.devnull, name).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        return diff[:MAX_TEXT] or None

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


def make_server(project: Path, port: int = 8765, config_file: Path | None = None) -> ThreadingHTTPServer:
    data = RunData(project, config_file)
    lock = threading.Lock()  # RunData's caches and log tails are not thread-safe
    resources = files("autohelix").joinpath("viewer_resources")

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
            if path == "/api/state":
                return 200, data.state()
            if match := re.fullmatch(r"/api/iteration/(\d{1,9})", path):
                return 200, data.iteration(int(match[1]))
            if match := re.fullmatch(r"/api/notes/([^/]+)", path):
                if note := data.note(unquote(match[1])):
                    return 200, note
            if match := re.fullmatch(r"/api/artifact/(\d{1,9})/([^/]+)", path):
                if found := data.artifact(int(match[1]), unquote(match[2])):
                    return 200, *found
            return 404, {"error": "not found"}

    # Threads, so one idle browser connection (e.g. a preconnect) cannot stall the page.
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server
