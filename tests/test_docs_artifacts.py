"""docs/ml-experiments.md's "One folder per iteration" recipe works as written: its
`paths.sh` (as the page has it), sourced by the agent, finds the last accepted
iteration in the copy's history and trains from its folder; sourced by a gate
command (whose copy has no history), it gives the same `$out` through
`AUTOHELIX_RUN_ID` / `AUTOHELIX_ITERATION`; a rejected iteration's model never
becomes the next start."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from autohelix.agents.mock import MockAgent
from autohelix.harness import Budget, Harness
from tests.helpers import configure

PAGE = Path(__file__).resolve().parent.parent / "docs" / "ml-experiments.md"


def paths_script() -> str:
    """The bash block after `<!-- recipe: paths -->`, unindented."""
    match = re.search(r"<!-- recipe: paths -->\n *```bash\n(.*?)\n *```", PAGE.read_text(), re.S)
    assert match, "no paths.sh recipe block in ml-experiments.md"
    return "\n".join(line.strip() for line in match.group(1).splitlines()).replace("\\\n", " ")


@pytest.mark.skipif(shutil.which("jq") is None, reason="the recipe uses jq")
def test_each_iteration_trains_from_the_last_accepted_iterations_folder(project, tmp_path, monkeypatch):
    artifacts = tmp_path / "data"
    paths = tmp_path / "paths.sh"
    paths.write_text(paths_script().replace("/data/x", str(artifacts)))
    # The agent's steps as the page gives them; "training": the start's weights plus
    # this iteration's number.
    train = (f'source {paths}\nrm -rf "$out"\nmkdir -p "$out"; '
             '{ cat "$start/weights" 2>/dev/null || echo base; } '
             '| sed "s/$/+$AUTOHELIX_ITERATION/" > "$out/weights"')
    real = MockAgent.run

    def run(self, copy_path, prompt, iteration, *args, **kwargs):
        subprocess.run(["bash", "-euc", train], cwd=copy_path, env=dict(os.environ), check=True)
        return real(self, copy_path, prompt, iteration, *args, **kwargs)

    monkeypatch.setattr(MockAgent, "run", run)
    configure(project, gates=[f'. {paths}; [ -z "${{start+set}}" ] && '
                              '{ [ "$AUTOHELIX_ITERATION" = 0 ] || [ -f "$out/weights" ]; }',
                              '[ "$AUTOHELIX_ITERATION" != 2 ]'])  # iteration 2 is rejected
    harness = Harness(project, land_mode="manual")
    harness.run(Budget(until=3), report=False)
    harness.close()
    assert [r.accepted for r in harness.history.load()] == [True, True, False, True]
    (run_folder,) = artifacts.iterdir()
    weights = {n: (run_folder / str(n) / "model" / "weights").read_text().strip() for n in (1, 2, 3)}
    assert weights == {1: "base+1", 2: "base+1+2", 3: "base+1+3"}  # 3 starts from 1, not 2
