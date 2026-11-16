"""The web pages' shared helpers (viewer_resources/common.js), run with node, by value."""

import json
import re
import shutil
import subprocess
from importlib.resources import files

import pytest

RESOURCES = files("autohelix").joinpath("viewer_resources")
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

RUNNER = """
const common = require(process.argv[1]);
const calls = JSON.parse(require('fs').readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(calls.map(([name, ...args]) => common[name](...args))));
"""


def js(*calls):
    """Each `(name, *args)` called in common.js; their results, in order."""
    out = subprocess.run(["node", "-e", RUNNER, str(RESOURCES.joinpath("common.js"))], input=json.dumps(calls),
                         capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


def test_numbers_as_the_console_writes_them():
    from autohelix.terminal import number

    values = [80200, 80200.0, 17481.5, 46.9, 1.23456, 0.123456, 120, -1234.5, 0]
    assert js(*[("fmtNumber", v) for v in values]) == [number(v) for v in values]
    assert js(("fmtNumber", None), ("fmtNumber", "12")) == ["—", "—"]


@pytest.mark.parametrize("value, baseline, shown", [
    (17481.5, 46.9, "373×"),
    (5, 100, "0.05×"),
    (117, 100, "+17%"),
    (80, 100, "-20%"),
    (99.6, 100, "-0.4%"),
    (100, 100, ""),
    (1, 0, ""),
    (None, 100, ""),
    # A negative baseline: the sign is the difference's (-1 -> -2 went down).
    (-2, -1, "-100%"),
    (-0.5, -1, "+50%"),
    (1, -1, "+200%"),
    (-20, -1, "-1900%"),  # no ×: "20×" would not say which way it moved
])
def test_change_against_the_baseline(value, baseline, shown):
    assert js(("fmtChange", value, baseline)) == [shown]


def test_change_matches_the_console_for_positive_values():
    from autohelix.terminal import change

    pairs = [(17481.5, 46.9), (5, 100), (117, 100), (80, 100), (99.6, 100), (0.5, 0.25), (10, 1), (1, 10)]
    assert js(*[("fmtChange", a, b) for a, b in pairs]) == [change(a, b) for a, b in pairs]


def test_durations_money_and_caps():
    from autohelix.formatting import format_cap, format_duration
    from autohelix.iteration_time import format_limit

    seconds = [0, 48, 62.7, 3599, 3700]
    assert js(*[("fmtDuration", s) for s in seconds]) == [format_duration(s) for s in seconds]
    assert js(("fmtDuration", None)) == [""]
    limits = [600, 7200, 5400, 725, 0]
    assert js(*[("fmtLimit", s) for s in limits]) == [format_limit(s) for s in limits]
    caps = [5, 0.015, 0.000000001, 12.5]
    assert js(*[("fmtCap", c) for c in caps]) == [format_cap(c) for c in caps]
    assert js(("fmtMoney", 4.339), ("fmtMoney", 4.339, True), ("fmtMoney", None)) == ["$4.34", "$4.34+", ""]


def test_time_left_as_the_console_says_it():
    from autohelix.run_table import time_cap

    running = {"state": "running", "max_time_seconds": 3600, "segment_started_at": 1000}
    cases = [(running, 1725), (running, 4599), (running, 5000), ({**running, "max_time_seconds": 5400}, 1000),
             ({**running, "state": "finished"}, 1725), ({**running, "max_time_seconds": None}, 1725)]
    assert js(*[("timeCap", status, now) for status, now in cases]) == [
        time_cap({"status": status}, now) for status, now in cases]
    assert js(("timeCap", running, 1725))[0] == "47m 55s left (--time 1h)"
    assert js(("timeCap", None)) == [""]


@pytest.mark.parametrize("run, running, text, hint", [
    ({"landing": {"commits": 0, "fast_forward": True}, "accepted": 2}, False, "landed", "on main"),
    ({"landing": {"commits": 0, "fast_forward": True}, "accepted": 0}, False, "—", "Nothing accepted yet"),
    ({"landing": {"commits": 3, "fast_forward": True}, "accepted": 3}, False, "3 to land", "3 commits: autohelix land 1q3w"),
    ({"landing": {"commits": 1, "fast_forward": True}, "accepted": 1}, True, "1 to land", "1 commit, on main when the run ends"),
    ({"landing": {"commits": 3, "fast_forward": True}, "accepted": 3, "land": "manual"}, True, "manual · 3 to land",
     "3 commits: autohelix land 1q3w"),
    ({"landing": {"commits": 3, "fast_forward": False}, "accepted": 3}, False, "3 diverged",
     "main has moved on: git merge autohelix/20261114-2333-sorting-1q3w"),
    ({"landing": None, "accepted": 3}, False, "—", "Branch main or the run's commits are not available"),
    ({"target_branch": None, "landing": None, "accepted": 1}, False, "detached",
     "Started on a detached HEAD: autohelix land 1q3w --into <branch>"),
])
def test_landing_words(run, running, text, hint):
    run = {"id": "20261114-2333-sorting-1q3w", "target_branch": "main", **run}
    words = js(("landingWords", run, running))[0]
    assert (words["text"], words["hint"]) == (text, hint)


def test_run_ids_and_notes():
    assert js(("shortId", "20261114-2333-sorting-1q3w")) == ["1q3w"]
    assert js(("splitRunId", "20261114-2333-sorting-1q3w"), ("splitRunId", "20261114-2333-1q3w")) == [
        {"name": "sorting-1q3w", "started": "2026-11-14 23:33"}, {"name": "Run 1q3w", "started": "2026-11-14 23:33"}]
    # run.json's notes come back as the text they are (the pages set them as text).
    run = {"why": "<b>bold</b>", "import": {"run": "k3f9"}, "archive_why": None}
    assert js(("runNotes", run)) == ["why: <b>bold</b> · continues from k3f9"]


def test_pages_set_html_only_from_server_rendered_markdown():
    # Everything else (run.json's notes, summaries, outputs) goes in as text.
    for name in ("common.js", "viewer.js", "dashboard.js"):
        source = RESOURCES.joinpath(name).read_text()
        assigned = re.findall(r"\.innerHTML\s*=\s*([\w.]+)", source)
        assert set(assigned) <= {"html", "o.html", "s.html"}, (name, assigned)


def test_both_pages_load_common_js_first():
    for page, script in (("index.html", "viewer.js"), ("dashboard.html", "dashboard.js")):
        scripts = re.findall(r'<script src="/([\w.]+)"', RESOURCES.joinpath(page).read_text())
        assert scripts == ["common.js", script]
