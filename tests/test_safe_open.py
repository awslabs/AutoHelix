"""Run logs named by untrusted metadata are opened only below logs/ or as an iteration's agent log or
judge/review transcripts, without symlinks or hard links."""

import io
import json
import os

import pytest
from rich.console import Console

from autohelix import live as W
from autohelix.replay import LogTail
from autohelix.run_status import RunStatus
from autohelix.safe_open import open_run_log

LINE = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "SECRET-TEXT"}]}}) + "\n"


def _log(run_paths):
    log = run_paths.agent_log(1)
    log.parent.mkdir(parents=True)
    return log


def _texts(tail):
    return [event.text for event in tail.read()]


def test_a_regular_log_is_read(run_paths):
    log = _log(run_paths)
    log.write_text(LINE)
    fd = open_run_log(run_paths, str(log))
    assert fd is not None
    os.close(fd)
    assert "SECRET-TEXT" in _texts(LogTail(log, opener=lambda: open_run_log(run_paths, str(log))))


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "outside"])
def test_planted_logs_are_refused(run_paths, tmp_path, kind):
    secret = tmp_path / "secret.txt"
    secret.write_text(LINE)
    log = _log(run_paths)
    if kind == "symlink":
        log.symlink_to(secret)
    elif kind == "hardlink":
        os.link(secret, log)
    elif kind == "fifo":
        os.mkfifo(log)
    else:
        log = secret  # a path outside the run's logs/
    assert open_run_log(run_paths, str(log)) is None
    tail = LogTail(log, opener=lambda: open_run_log(run_paths, str(log)))
    with pytest.raises(OSError):
        tail.read()


def test_a_symlinked_folder_inside_logs_is_refused(run_paths, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "agent.log").write_text(LINE)
    folder = run_paths.logs / "iter-1"
    folder.parent.mkdir(parents=True, exist_ok=True)
    folder.symlink_to(elsewhere)
    assert open_run_log(run_paths, str(folder / "agent.log")) is None


def test_a_symlinked_iteration_folder_is_refused(run_paths, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "agent.log").write_text(LINE)
    run_paths.iterations.mkdir(parents=True)
    run_paths.iteration_dir(1).symlink_to(elsewhere)
    assert open_run_log(run_paths, str(run_paths.agent_log(1))) is None


@pytest.mark.parametrize("name", [
    "prompt.md", "record.json", "observe/bench/stdout.txt", "gates/1/stdout.txt", "gates/1/command.txt",
    "stop/2/stderr.txt", "gates/0/ask.log", "gates/01/ask.log", "gates/x/ask.log", "gates/1/sub/ask.log",
    "gates/1/asking.log", "gates/1/ask-.log", "gates/1/ask-x.log", "gates/ask.log", "observe/.hidden/review.log",
    "observe/bench/sub/review.log", "other/1/ask.log", "notes/ask.log", "ask.log",
])
def test_only_logs_of_an_iteration_folder_open(run_paths, name):
    for folder in (run_paths.iteration_dir(1), run_paths.attempt_dir(1, 2)):
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(LINE)
        assert run_paths.relative_log(str(path)) is None and open_run_log(run_paths, str(path)) is None


@pytest.mark.parametrize("name", [
    "agent.log", "gates/1/ask.log", "gates/12/ask-2.log", "gates/3/review.log", "stop/1/ask.log",
    "stop/2/review-10.log", "observe/bench/review.log", "observe/review/ask-3.log",
])
def test_an_iterations_logs_open(run_paths, name):
    for folder in (run_paths.iteration_dir(1), run_paths.iteration_dir(1000), run_paths.attempt_dir(3, 2)):
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(LINE)
        relative = str(path.relative_to(run_paths.run_dir))
        assert run_paths.relative_log(str(path)) == relative == run_paths.relative_log(relative)
        fd = open_run_log(run_paths, relative)
        assert fd is not None
        os.close(fd)


@pytest.mark.parametrize("value", [
    "iterations/1/agent.log", "iterations/01/agent.log", "iterations/abc/agent.log", "iterations/001",
    ".internal/attempts/001/agent.log", ".internal/attempts/001-0/agent.log", ".internal/attempts/1-1/agent.log",
    ".internal/attempts/001-1/record.json", ".internal/notes/1/ask.log", ".internal/usage/001.jsonl",
    ".internal/status.json", "history.jsonl", "../outside/logs/x.log",
])
def test_other_paths_are_refused(run_paths, value):
    assert run_paths.relative_log(value) is None


def test_a_planted_link_on_the_way_to_a_transcript_is_refused(run_paths, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "1").mkdir(parents=True)
    (elsewhere / "1" / "ask.log").write_text(LINE)
    run_paths.iteration_dir(1).mkdir(parents=True)
    (run_paths.iteration_dir(1) / "gates").symlink_to(elsewhere)
    assert open_run_log(run_paths, "iterations/001/gates/1/ask.log") is None
    log = run_paths.stop_dir(1, 1) / "ask.log"
    log.parent.mkdir(parents=True)
    os.link(elsewhere / "1" / "ask.log", log)  # a hard link: refused too
    assert open_run_log(run_paths, "iterations/001/stop/1/ask.log") is None


def _follow(tmp_path, log):
    status = RunStatus(tmp_path)
    status.start()
    status.phase("agent", 1)
    status.note(log=str(log))
    out = io.StringIO()
    follower = W.LogFollower(tmp_path, "claude", Console(file=out, width=120))
    follower.poll(status.data)
    with Console(file=out, width=120) as console:
        if follower.display is not None:
            console.print(follower.display)
    return out.getvalue()


def test_terminal_view_follows_a_regular_log(tmp_path, run_paths):
    log = _log(run_paths)
    log.write_text(LINE)
    assert "SECRET-TEXT" in _follow(tmp_path, log)


def test_terminal_view_ignores_a_status_log_outside_the_run(tmp_path, run_paths):
    """The status file is run state an agent can write: it must not make `view` show any file."""
    secret = tmp_path / "secret.txt"
    secret.write_text(LINE)
    assert "SECRET-TEXT" not in _follow(tmp_path, secret)


@pytest.mark.parametrize("planted", [False, True])
def test_web_viewer_reads_only_regular_logs(tmp_path, run_paths, planted):
    from autohelix.viewer import RunData

    secret = tmp_path / "secret.txt"
    secret.write_text(LINE)
    log = _log(run_paths)
    if planted:
        os.link(secret, log)  # a hard link to a file elsewhere on the same disk
    else:
        log.write_text(LINE)
    events = RunData(tmp_path, run_id="test")._events(log)
    assert any("SECRET-TEXT" in event["text"] for event in events) is not planted
