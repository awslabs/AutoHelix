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


def test_numbers():
    values = [80200, 17481.5, 46.9, 1.23456, 0.123456, 120, -1234.5, 0]
    assert js(*[("fmtNumber", v) for v in values]) == [
        "80200.0", "17481.5", "46.9", "1.235", "0.1235", "120.0", "-1234.5", "0"]
    assert js(("fmtNumber", None), ("fmtNumber", "12")) == ["—", "—"]


@pytest.mark.parametrize("value, baseline, shown", [
    (17481.5, 46.9, "↑ 373x"),
    (5, 100, "↓ 20x"),
    (117, 100, "↑ 17.0%"),
    (80, 100, "↓ 20.0%"),
    (99.6, 100, "↓ 0.4%"),
    (100, 100, ""),
    (1, 0, ""),
    (None, 100, ""),
    # A negative baseline: the arrow is the difference's (-1 -> -2 went down).
    (-2, -1, "↓ 100.0%"),
    (-0.5, -1, "↑ 50.0%"),
    (1, -1, "↑ 200.0%"),
    (-20, -1, "↓ 1900.0%"),  # no x: "20x" would not say which way it moved
])
def test_change_against_the_baseline(value, baseline, shown):
    assert js(("formatDelta", value, baseline)) == [shown]


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


def test_run_ids_and_notes():
    assert js(("splitRunId", "20261114-2333-sorting-1q3w"), ("splitRunId", "20261114-2333-1q3w")) == [
        {"name": "sorting-1q3w", "started": "2026-11-14 23:33"}, {"name": "Run 1q3w", "started": "2026-11-14 23:33"}]
    # run.json's notes come back as the text they are (the pages set them as text).
    run = {"why": "<b>bold</b>", "import": {"run": "k3f9"}, "archive_why": None}
    assert js(("runNotes", run)) == ["Why: <b>bold</b> · continues from k3f9"]


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


THEME_RUNNER = """
// A page with the header's theme button and logo, the system in dark mode, and the
// stored choice from argv; click the button three times.
const nodes = {};
const node = (id) => (nodes[id] ??= { id, dataset: {}, listeners: {}, addEventListener(type, f) { this.listeners[type] = f; } });
const store = { 'autohelix-theme': process.argv[2] };
globalThis.document = { getElementById: node, documentElement: node('html') };
globalThis.localStorage = { getItem: (k) => store[k] ?? null, setItem: (k, v) => { store[k] = v; } };
globalThis.matchMedia = () => ({ matches: true, addEventListener() {} });
const { themeButton } = require(process.argv[1]);
const seen = () => [nodes.theme.textContent, nodes.html.dataset.theme ?? 'system', nodes.logo.src, store['autohelix-theme']];
themeButton();
const states = [seen()];
for (let i = 0; i < 3; i++) { nodes.theme.listeners.click(); states.push(seen()); }
process.stdout.write(JSON.stringify(states));
"""


def test_theme_button_cycles_and_remembers():
    def clicks(stored):
        out = subprocess.run(["node", "-e", THEME_RUNNER, str(RESOURCES.joinpath("common.js")), stored],
                             capture_output=True, text=True, check=True, timeout=30)
        return json.loads(out.stdout)

    # Auto follows the (dark) system; Light and Dark force one, and the logo follows.
    assert clicks("") == [
        ["◐ Auto", "system", "/logo-dark.svg", ""],
        ["☀ Light", "light", "/logo-light.svg", "light"],
        ["☾ Dark", "dark", "/logo-dark.svg", "dark"],
        ["◐ Auto", "system", "/logo-dark.svg", "auto"],
    ]
    # A stored choice is where it starts; an unknown one is Auto.
    assert clicks("light")[0][:2] == ["☀ Light", "light"]
    assert clicks("sepia")[0][:2] == ["◐ Auto", "system"]


def test_both_pages_have_the_theme_button():
    for page in ("index.html", "dashboard.html"):
        ids = re.findall(r'\sid="([\w-]+)"', RESOURCES.joinpath(page).read_text())
        assert {"theme", "logo", "status"} <= set(ids), page
