# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""`autohelix view` / `autohelix dashboard`: a small live web app, served on 127.0.0.1.

Two pages share one server: `/` lists the project's runs, `/run/<id>/` shows one
run. The pages poll a few JSON routes. There are no write routes. Every file read
is derived from a run id that must name an existing run directory, an iteration
number, or a file name that must match an entry in a fixed directory listing
(notes/, observations/iter-N/). The unprefixed `/api/...` routes serve the
server's default run.

`write_static` saves the same page as one file, output/report.html, with the
route data built in.
"""

import json
import os
import re
import socket
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
from autohelix.config import load_config
from autohelix.costs import summarize_costs
from autohelix.history import History
from autohelix.runs import RunPaths, git
from autohelix.replay import LogTail
from autohelix.report import read_report_info
from autohelix.run_status import read_run_status
from autohelix.state import read_stored_config_path

MAX_TEXT = 256 * 1024
MAX_EVENTS = 3000  # merged events kept per log; tool output is clipped to keep this small
MAX_ARTIFACT = 20 * 1024 * 1024
COMMIT = re.compile(r"[0-9a-f]{7,64}")
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}
LOOPBACK = {"127.0.0.1", "localhost", "[::1]"}
ASSETS = {
    "/": ("dashboard.html", "text/html; charset=utf-8"),
    "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
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
        self._index_dir: str | None = None
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
        self._refresh_current()
        config, config_path = self._config()
        records = self.history.load() if self.paths.run_id else []
        total_cost = summarize_costs([usage for row in records for usage in row.cost_usages])
        subjects = self._subjects([r.commit for r in records if r.commit])
        iterations = [
            {"iteration": r.iteration, "accepted": r.accepted, "metrics": r.metrics,
             "summary": r.reason if r.reason != "baseline" else None,
             "cost": r.cost.cost_usd, "cost_incomplete": r.cost.incomplete,
             "seconds": (r.usage.get("wall_clock_ms") or 0) / 1000 or None}
            for r in records
        ]
        for item, record in zip(iterations, records):
            item["summary"] = item["summary"] or subjects.get(record.commit)
        return {
            "title": getattr(config, "title", None) or self.project.name,
            "project": self.project.name,
            "goal": config.goal if config else None,
            "directions": config.metric_directions() if config else {},
            "status": read_run_status(self.project, self.paths.run_id),
            "run_id": self.paths.run_id,
            "config": self._config_summary(config, config_path),
            "notes_files": sorted((e.name for e in _files(self.root / "notes")), key=_note_order) if self.paths.run_id else [],
            "artifacts": self._artifacts() if self.paths.run_id else {},
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

    def iteration(self, n: int, logs: bool = True, diff: bool = True) -> dict:
        self._refresh_current()
        if not self.paths.run_id:
            return {"iteration": n, "failure_output": None, "notes": None, "review": None,
                    "diff": None, "agent": [], "calls": [], "agent_clipped": False, "reviewer": []}
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
        # may be left over from an earlier, failed attempt at the same iteration.
        notes = [self.root / "notes" / f"iter-{n}.md", self._worktree(n) / ".autohelix" / "notes" / f"iter-{n}.md"]
        if record is None:
            notes.reverse()
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
        review_events = [event for call in phases if call["role"] == "reviewer" for event in call["events"]]
        return {
            "iteration": n,
            "failure_output": record.failure_output if record else None,
            "notes": _markdown(next(filter(None, map(_read_text, notes)), None)),
            "review": _markdown(_read_text(self.root / "reviews" / f"iter-{n}.md")),
            "diff": changes,
            "agent": agent_events,
            "calls": phases,
            "agent_clipped": any(self.paths.run_dir / call["log"] in self._clipped
                                 for call in phases if call.get("log")),
            "reviewer": review_events,
        }

    def snapshot(self) -> dict:
        """The page's data keyed by route, for report.html: no logs or captured output,
        and one diff, baseline to final code, on the last accepted iteration."""
        state = {**self.state(), "artifacts": {}}
        accepted = [r["iteration"] for r in state["iterations"] if r["iteration"] > 0 and r["accepted"]]
        final = accepted[-1] if accepted else None
        routes = {"/api/state": state}
        for r in state["iterations"]:
            n = r["iteration"]
            routes[f"/api/iteration/{n}"] = self.iteration(n, logs=False, diff=n == final)
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
        if not self.paths.run_id:
            return None
        entry = next((e for e in _files(self.root / "notes") if e.name == name), None)
        return {"name": name, "html": _markdown(_read_text(Path(entry.path)))} if entry else None

    def artifact(self, n: int, name: str) -> tuple[bytes, str] | None:
        if not self.paths.run_id:
            return None
        entry = next((e for e in _files(self.root / "observations" / f"iter-{n}") if e.name == name), None)
        if entry is None or entry.stat().st_size > MAX_ARTIFACT:
            return None
        # Anything that is not a plain image is served as text, so captured
        # HTML/SVG/JS can never run in the viewer's origin.
        mime = IMAGE_TYPES.get(Path(name).suffix.lower(), "text/plain; charset=utf-8")
        return Path(entry.path).read_bytes(), mime

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
                diff = repo.git("diff", "--no-ext-diff", "--no-textconv", base, self.paths.pin, "--", timeout=5)
            else:
                if self._index_dir is None:
                    import tempfile

                    self._index_dir = tempfile.mkdtemp(prefix="autohelix-view-")
                index = Path(self._index_dir) / f"{self.paths.run_id}-{copy.name}.index"
                if not index.exists():
                    repo.git("read-tree", base, index=index, timeout=5)
                repo.git("add", "-A", "--", ".", work_tree=copy, index=index, timeout=5)
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


RUN_ID = r"[a-z0-9][a-z0-9-]{0,36}"


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
        home = Path.home()
        path = f"~/{self.project.relative_to(home)}" if self.project.is_relative_to(home) else str(self.project)
        # What the checkout is on: the reference point for each run's landing state.
        branch = git(self.project, "symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip() or None
        commit = git(self.project, "rev-parse", "--short", "HEAD", check=False).stdout.strip() or None
        return {"project": self.project.name, "host": socket.gethostname().split(".")[0],
                "path": path, "path_full": str(self.project), "branch": branch, "commit": commit, "runs": rows}

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
            summary = {"file": relative,
                       "directions": config.metric_directions(),
                       "max_iterations": config.max_iterations, "max_cost_usd": config.max_cost_usd,
                       "max_time_seconds": config.max_time_seconds}
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
        config = self._config(data.get("config_path"))

        attempts = [r for r in records if r.iteration > 0]
        cost = summarize_costs([usage for r in records for usage in r.cost_usages])
        tip = refs.get(paths.ref) or data.get("abandoned_tip")
        target = data.get("target_branch")
        target_tip = refs.get(f"refs/heads/{target}") if target else None
        try:
            history_mtime = paths.history.stat().st_mtime
        except OSError:
            history_mtime = None
        activity = [data.get("created_at"), history_mtime, (status or {}).get("updated_at")]
        return {
            "id": run_id,
            "name": data.get("name"),
            "lifecycle": data.get("lifecycle"),
            "created_at": data.get("created_at"),
            "last_activity": max((t for t in activity if isinstance(t, (int, float))), default=None),
            "config": {"file": config.get("file")},
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
        if match := re.fullmatch(r"/notes/([^/]+)", path):
            if note := rd.note(unquote(match[1])):
                return 200, note
        if match := re.fullmatch(r"/artifact/(\d{1,9})/([^/]+)", path):
            if found := rd.artifact(int(match[1]), unquote(match[2])):
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
