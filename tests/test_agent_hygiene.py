"""What the main agent is given: its environment (no `AUTOHELIX_PROJECT` or
`AUTOHELIX_RUN_DIR`, `AUTOHELIX_ITERATION` added) and a copy without the config file and
its prompt template. Hygiene, not a boundary: commands keep both variables, and the
removal is never committed."""

import os

from autohelix.agents.mock import MockAgent
from autohelix.harness import Budget, Harness
from autohelix.iteration import _remove_file
from tests.helpers import CONFIG, config_file, configure, git, latest_run

TEMPLATE = CONFIG.replace("config.yaml", "prompt.md")
VARIABLES = ("AUTOHELIX_PROJECT", "AUTOHELIX_RUN_DIR", "AUTOHELIX_RUN_ID", "AUTOHELIX_ITERATION")


def _watch_agent(monkeypatch, also=None):
    """Record what each main-agent call sees: its variables and its copy's files."""
    seen = []
    real = MockAgent.run

    def run(self, copy_path, prompt, iteration, *args, **kwargs):
        experiment = copy_path / CONFIG.rsplit("/", 1)[0]
        seen.append({"env": {name: os.environ.get(name) for name in VARIABLES},
                     "files": sorted(p.name for p in copy_path.iterdir()),
                     "experiment": sorted(p.name for p in experiment.iterdir()) if experiment.is_dir() else None})
        if also:
            also(copy_path)
        return real(self, copy_path, prompt, iteration, *args, **kwargs)

    monkeypatch.setattr(MockAgent, "run", run)
    return seen


def test_the_agent_gets_its_run_and_iteration_but_not_the_project_or_run_folder(project, monkeypatch):
    # Inherited values (the harness started inside another run's command) don't reach it either.
    monkeypatch.setenv("AUTOHELIX_PROJECT", "/elsewhere")
    monkeypatch.setenv("AUTOHELIX_RUN_DIR", "/elsewhere/run")
    configure(project, gates=[f'[ "$AUTOHELIX_PROJECT" = "{project.resolve()}" ]',
                              '[ -d "$AUTOHELIX_RUN_DIR" ] && [ "$AUTOHELIX_ITERATION" -ge 0 ]'])
    seen = _watch_agent(monkeypatch)
    harness = Harness(project, land_mode="manual")
    harness.run(Budget(until=2), report=False)
    harness.close()
    run = latest_run(project)
    assert [s["env"] for s in seen] == [
        {"AUTOHELIX_PROJECT": None, "AUTOHELIX_RUN_DIR": None, "AUTOHELIX_RUN_ID": run.run_id,
         "AUTOHELIX_ITERATION": str(n)} for n in (1, 2)]
    records = harness.history.load()
    assert [r.accepted for r in records] == [True, True, True]  # the gates saw both variables
    assert os.environ["AUTOHELIX_PROJECT"] == "/elsewhere"  # restored after the call
    assert "AUTOHELIX_ITERATION" not in os.environ


def test_gate_commands_keep_the_project_and_run_folder(project):
    configure(project, gates=['[ "$AUTOHELIX_PROJECT" = "$(git -C "$AUTOHELIX_PROJECT" rev-parse --show-toplevel)" ]',
                              '[ -f "$AUTOHELIX_RUN_DIR/run.json" ]'])
    harness = Harness(project, land_mode="manual")
    harness.run(Budget(until=1), report=False)
    harness.close()
    assert [r.accepted for r in harness.history.load()] == [True, True]


def test_the_agent_copy_has_no_config_or_template_and_the_removal_is_never_committed(project, monkeypatch):
    configure(project, prompt_template="prompt.md")
    (config_file(project).parent / "prompt.md").write_text("Goal: {{ goal }}\n")
    config = config_file(project).read_text()
    git(project, "add", "-f", config_file(project).parent)  # an experiment committed with the project
    git(project, "commit", "-qm", "experiment")
    seen = _watch_agent(monkeypatch)
    harness = Harness(project, land_mode="manual")
    harness.run(Budget(until=1), report=False)
    harness.close()
    [call] = seen
    assert not call["experiment"]  # its config and template removed
    assert "code.py" in call["files"]
    record = harness.history.load()[-1]
    assert record.accepted and record.commit
    assert git(project, "show", f"{record.commit}:{CONFIG}") + "\n" == config
    assert git(project, "show", f"{record.commit}:{TEMPLATE}") == "Goal: {{ goal }}"
    assert record.discarded == [] and record.discarded_frozen == []  # the harness's removal, not the agent's
    assert config_file(project).read_text() == config  # the checkout is untouched


def test_a_config_the_agent_writes_back_is_discarded_as_frozen(project, monkeypatch):
    def write_back(copy):
        (copy / CONFIG).parent.mkdir(parents=True, exist_ok=True)
        (copy / CONFIG).write_text("goal: mine\n")

    git(project, "add", "-f", CONFIG)  # committed: a private one is ignored in the copy too
    git(project, "commit", "-qm", "experiment")
    _watch_agent(monkeypatch, also=write_back)
    harness = Harness(project, land_mode="manual")
    harness.run(Budget(until=1), report=False)
    harness.close()
    record = harness.history.load()[-1]
    assert record.discarded_frozen == [CONFIG]
    assert "goal: mine" not in git(project, "show", f"{record.commit}:{CONFIG}")


def test_removal_never_follows_a_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "config.yaml").write_text("keep")
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / "conf").symlink_to(outside)
    assert not _remove_file(copy, "conf/config.yaml")
    assert (outside / "config.yaml").read_text() == "keep"
    assert not _remove_file(copy, "missing.yaml")
    (copy / "dir.yaml").mkdir()
    assert not _remove_file(copy, "dir.yaml") and (copy / "dir.yaml").is_dir()
    (copy / "link.yaml").symlink_to(outside / "config.yaml")
    assert _remove_file(copy, "link.yaml")  # the link itself, never its target
    assert not os.path.lexists(copy / "link.yaml") and (outside / "config.yaml").read_text() == "keep"
