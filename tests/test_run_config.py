"""One config per run: the stored copy, the loader's stored form, resume's comparison,
and the config and template files always frozen."""

import stat

import pytest
import yaml
from click.testing import CliRunner

from autohelix.cli import main
from autohelix.config import config_changes, inlined, load_config, stored_config_text
from autohelix.harness import Budget, Harness
from autohelix.history import History
from autohelix.prompt_template import load_template
from autohelix.runs import RunPaths, git, short_id
from autohelix.workspace import Scope

CONFIG = """\
# Why this goal: setup's reasoning, as comments.
goal: Make a checked change  # the title
report: false
agent:
  backend: mock
  settings:
    change_file: code.py
    change_content: "# iteration {iteration}"
"""


def commit(project, message="change"):
    git(project, "add", "-A")
    git(project, "commit", "-qm", message)


def cli(project, *args):
    return CliRunner().invoke(main, [*args, "-p", str(project)])


@pytest.fixture
def started(project):
    """The shared project with a commented config, and one finished run of it."""
    (project / "autohelix.yaml").write_text(CONFIG)
    commit(project, "config")
    result = cli(project, "run", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    return RunPaths(project)


# --- the stored copy ---


def test_a_config_without_template_is_stored_verbatim(started):
    assert started.config.read_text() == CONFIG


def test_the_template_line_is_replaced_and_comments_kept(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "p.md").write_text("  Indented first line\n{{ goal }}\n\n")
    config = tmp_path / "c.yaml"
    config.write_text("# keep me\ngoal: g  # and me\nprompt_template: prompts/p.md  # gone\nreport: false\n")
    text = stored_config_text(config)
    assert text.startswith("# keep me\ngoal: g  # and me\nprompt_template:  # stored when the run started\n")
    assert text.endswith("report: false\n")
    assert yaml.safe_load(text) == {"goal": "g", "report": False, "prompt_template": {
        "file": "prompts/p.md", "text": "  Indented first line\n{{ goal }}\n\n"}}
    assert yaml.safe_load(text) == inlined(config)


@pytest.mark.parametrize("template", ["no newline", "", "a\r\nb\r\n", "\n\n  x\n", "# h\n- k: 'v'\n"])
def test_awkward_templates_round_trip(tmp_path, template):
    (tmp_path / "p.md").write_bytes(template.encode())
    config = tmp_path / "c.yaml"
    config.write_text("goal: g\nprompt_template: p.md")
    assert yaml.safe_load(stored_config_text(config))["prompt_template"]["text"] == (tmp_path / "p.md").read_text()


@pytest.mark.parametrize("source", ["{goal: g, prompt_template: p.md}\n", "goal: g\nprompt_template:\n  p.md\n"])
def test_a_template_line_that_cannot_be_replaced_falls_back_to_a_dump(tmp_path, source):
    (tmp_path / "p.md").write_text("line one\nline two\n")
    config = tmp_path / "c.yaml"
    config.write_text(source)
    text = stored_config_text(config)
    assert text.startswith("# stored when the run started from c.yaml")
    assert yaml.safe_load(text) == {"goal": "g", "prompt_template": {"file": "p.md", "text": "line one\nline two\n"}}


# --- the loader's stored form ---


def test_the_stored_form_gives_the_template_text(tmp_path):
    source = tmp_path / "autohelix.yaml"
    stored = tmp_path / "stored.yaml"
    stored.write_text("goal: g\nprompt_template:\n  file: prompts/p.md\n  text: 'Hi {{ goal }}'\n")
    config, _ = load_config(tmp_path, stored, source=source)
    assert load_template(config) == "Hi {{ goal }}"
    assert config.prompt_template == (tmp_path / "prompts" / "p.md").resolve()  # next to the source
    assert not [i for i in config.validate() if i.level == "error"]  # the file needn't exist
    assert config.protected == ["autohelix.yaml", "prompts/p.md"]


@pytest.mark.parametrize("template, error", [
    ({"file": "p.md", "text": "t", "extra": 1}, "Unknown key prompt_template.extra (known: file, text)"),
    ({"file": "p.md", "text": 3}, "a stored prompt_template needs file: (its path) and text: (its contents)"),
    ({"text": "t"}, "a stored prompt_template needs file:"),
])
def test_a_malformed_stored_form_is_a_config_error(tmp_path, template, error):
    (tmp_path / "c.yaml").write_text(yaml.safe_dump({"goal": "g", "prompt_template": template}))
    config, _ = load_config(tmp_path, tmp_path / "c.yaml")
    assert any(error in i.message for i in config.validate() if i.level == "error")


def test_protected_names_are_shown_plain_and_matched_escaped(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "p[1].md").write_text("t")
    (tmp_path / "outside.md").write_text("t")
    (project / "a.yaml").write_text("goal: g\nprompt_template: p[1].md\n")
    (project / "b.yaml").write_text("goal: g\nprompt_template: ../outside.md\n")
    config = load_config(project, project / "a.yaml")[0]
    assert config.protected == config.frozen_names() == ["a.yaml", "p[1].md"]
    assert config.frozen_patterns() == ["a.yaml", "p[[]1].md"]
    assert not Scope(frozen=tuple(config.frozen_patterns())).allows("p[1].md")
    assert Scope(frozen=tuple(config.frozen_patterns())).allows("p1.md")
    config = load_config(project, project / "b.yaml")[0]
    assert config.protected == ["b.yaml"]
    assert config.frozen_patterns() == ["b.yaml"] and config.frozen == []


def test_a_symlinked_config_and_template_are_frozen_as_named_and_as_resolved(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "c.yaml").write_text("goal: g\nprompt_template: ../p.md\n")
    (tmp_path / "real" / "t.md").write_text("t")
    (tmp_path / "autohelix.yaml").symlink_to("real/c.yaml")
    (tmp_path / "p.md").symlink_to("real/t.md")
    config = load_config(tmp_path, tmp_path / "autohelix.yaml")[0]
    # The template's path resolves next to the config's target (real/../p.md), itself a link.
    assert config.protected == ["autohelix.yaml", "real/c.yaml", "p.md", "real/t.md"]
    scope = Scope(frozen=tuple(config.frozen_patterns()))
    assert not any(scope.allows(name) for name in config.protected)


# --- resume compares, refuses before spending ---


def test_a_comment_or_formatting_edit_resumes(started):
    (started.project / "autohelix.yaml").write_text(
        "# reworded reasoning\n" + CONFIG.replace("report: false", "report:   false   # still off"))
    commit(started.project)
    result = cli(started.project, "resume", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert History(started.project, started.run_id).get_last_iteration() == 2
    assert started.config.read_text() == CONFIG  # the stored copy never changes


def refused(paths, result, keys):
    short = short_id(paths.run_id)
    text = " ".join(result.output.split())
    assert result.exit_code == 1, result.output
    assert (f"autohelix.yaml changed since run {short} started (changed: {keys}); the run keeps the config it "
            f"started with. To apply the new config, start a new run that continues from this one: "
            f"`autohelix run --import {short}`. To continue this run, restore the file.") in text
    assert History(paths.project, paths.run_id).get_last_iteration() == 1  # nothing spent


def test_a_value_edit_refuses(started):
    (started.project / "autohelix.yaml").write_text(CONFIG.replace("Make a checked change", "Something else"))
    commit(started.project)
    refused(started, cli(started.project, "resume", "-n", "1", "--no-report"), "goal")
    refused(started, cli(started.project, "resume", short_id(started.run_id), "--no-report"), "goal")


def test_a_template_edit_refuses(project):
    (project / "prompt.md").write_text("Do it: {{ goal }}\n")
    (project / "autohelix.yaml").write_text(CONFIG + "prompt_template: prompt.md\n")
    commit(project, "template")
    assert cli(project, "run", "-n", "1", "--no-report").exit_code == 0
    paths = RunPaths(project)
    assert "prompt_template:  # stored when the run started\n  file: prompt.md\n" in paths.config.read_text()
    assert "Do it: Make a checked change" in paths.prompt(1).read_text()
    (project / "prompt.md").write_text("Do it differently: {{ goal }}\n")
    refused(paths, cli(project, "resume", "-n", "1", "--no-report"), "prompt_template")
    (project / "prompt.md").unlink()
    (project / "prompt.md").mkdir()  # there, but unreadable: a change too
    result = cli(project, "resume", "-n", "1", "--no-report")
    assert result.exit_code == 1 and "it no longer reads: Cannot read prompt_template prompt.md" in result.output
    (project / "prompt.md").rmdir()
    (project / "prompt.md").write_text("Do it: {{ goal }}\n")  # restored
    assert cli(project, "resume", "-n", "1", "--no-report").exit_code == 0


def test_a_deleted_template_continues_but_other_keys_are_compared(project):
    (project / "prompt.md").write_text("Do it: {{ goal }}\n")
    (project / "autohelix.yaml").write_text(CONFIG + "prompt_template: prompt.md\n")
    commit(project, "template")
    assert cli(project, "run", "-n", "1", "--no-report").exit_code == 0
    paths = RunPaths(project)
    git(project, "rm", "-q", "prompt.md")
    commit(project, "drop template")
    (project / "autohelix.yaml").write_text(CONFIG.replace("Make a checked change", "Other") + "prompt_template: prompt.md\n")
    refused(paths, cli(project, "resume", "-n", "1", "--no-report"), "goal")
    (project / "autohelix.yaml").write_text(CONFIG + "prompt_template: prompt.md\n")
    result = cli(project, "resume", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert (f"prompt.md is gone; run {short_id(paths.run_id)} continues with the prompt template it stored "
            "when it started.") in " ".join(result.output.split())
    assert "Do it: Make a checked change" in paths.prompt(2).read_text()


@pytest.mark.parametrize("edit, changed", [
    ("gates: []\nscope: {editable: null}\nstop:\n", None),
    ("prompt_template: ./prompt.md\n", None),
    ("report: true\n", "report"),  # a default written out is a change: the message names it
])
def test_empty_values_and_path_spellings_are_not_changes(project, edit, changed):
    (project / "prompt.md").write_text("Do it: {{ goal }}\n")
    (project / "autohelix.yaml").write_text(CONFIG.replace("report: false\n", "") + "prompt_template: prompt.md\n")
    commit(project, "template")
    assert cli(project, "run", "-n", "1", "--no-report").exit_code == 0
    paths = RunPaths(project)
    text = CONFIG.replace("report: false\n", "") + "prompt_template: prompt.md\n"
    if edit.startswith("prompt_template"):
        text = text.replace("prompt_template: prompt.md\n", edit)
    else:
        text += edit
    (project / "autohelix.yaml").write_text(text)
    result = cli(project, "resume", "-n", "1", "--no-report")
    if changed:
        refused(paths, result, changed)
    else:
        assert result.exit_code == 0, result.output


def test_config_changes_normalizes_nested_empties():
    assert config_changes({"scope": {"editable": None}, "gates": [], "goal": "g"}, {"goal": "g"}) == []
    assert config_changes({"agent": {"settings": {}}}, {"agent": None}) == []
    assert config_changes({"prompt_template": {"file": "a/./p.md", "text": "t"}},
                          {"prompt_template": {"file": "a/p.md", "text": "t"}}) == []
    assert config_changes({"agent": {"timeout": "10m"}}, {"agent": {"timeout": 600}}) == ["agent"]


def test_a_deleted_config_continues_from_the_stored_copy(started):
    git(started.project, "rm", "-q", "autohelix.yaml")
    git(started.project, "commit", "-qm", "drop config")
    harness = Harness(started.project, run_id=started.run_id)
    assert harness.config.goal == "Make a checked change"
    harness.run(Budget(until=2), report=False)
    assert History(started.project, started.run_id).get_last_iteration() == 2


def test_the_stored_copy_is_read_only_and_delete_removes_it(started):
    assert stat.S_IMODE(started.config.stat().st_mode) == 0o444
    result = cli(started.project, "delete", short_id(started.run_id), "--force")
    assert result.exit_code == 0, result.output
    assert not started.run_dir.exists()


def test_a_new_run_is_not_compared_with_a_file_edited_while_it_started(project, monkeypatch):
    import autohelix.runs as runs

    (project / "autohelix.yaml").write_text(CONFIG)
    commit(project, "config")

    def store_then_edit(config):
        text = stored_config_text(config)
        config.write_text(CONFIG.replace("Make a checked change", "Edited meanwhile"))
        return text

    monkeypatch.setattr(runs, "stored_config_text", store_then_edit)
    harness = Harness(project, new=True)
    assert harness.config.goal == "Make a checked change"  # its stored copy
    harness.close()


def test_a_run_without_a_stored_config_refuses(started):
    started.config.unlink()
    with pytest.raises(RuntimeError, match=r"has no stored config .* `autohelix run --import "
                                           rf"{short_id(started.run_id)}`"):
        Harness(started.project, run_id=started.run_id)


def test_judges_read_the_stored_copy(project, tmp_path):
    seen = tmp_path / "seen"
    (project / "autohelix.yaml").write_text(CONFIG + f'gates:\n  - cat "$AUTOHELIX_CONFIG" > {seen}\n')
    commit(project, "config")
    harness = Harness(project)
    harness.run(Budget(until=1), report=False)
    assert seen.read_text() == harness.paths.config.read_text() == (project / "autohelix.yaml").read_text()
    assert harness.config_file == project / "autohelix.yaml"  # the source, for names and messages


# --- the config and template files are always frozen ---


@pytest.mark.parametrize("target", ["autohelix.yaml", "prompt.md"])
def test_an_agent_edit_to_the_config_or_template_is_reverted(project, target):
    (project / "prompt.md").write_text("Do it: {{ goal }}\nFrozen: {{ frozen | join(', ') }}\n")
    (project / "autohelix.yaml").write_text(CONFIG.replace("code.py", target) + "prompt_template: prompt.md\n")
    commit(project, "config")
    harness = Harness(project, land_mode="manual")
    harness.run(Budget(until=1), report=False)
    record = History(project, harness.paths.run_id).load()[1]
    assert record.discarded == record.discarded_frozen == [target]
    assert "Frozen: autohelix.yaml, prompt.md" in harness.paths.prompt(1).read_text()
    tip = git(project, "rev-parse", harness.paths.ref).stdout.strip()
    assert git(project, "diff", "--name-only", harness.run_info["base_commit"], tip).stdout == ""
    assert harness.config.frozen == []  # `scope:` unset: its warning still applies (`scope_warning`)
