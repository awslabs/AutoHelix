"""One config per run: the stored copy (the config verbatim, its template beside it),
resume's comparison of parsed configs, and the config and template files always frozen."""

import stat

import pytest
import yaml

from autohelix.config import config_changes, load_config
from autohelix.harness import Budget, Harness
from autohelix.history import History
from autohelix.prompt_template import load_template
from autohelix.runs import RunPaths, git, load_run_config, short_id
from autohelix.workspace import Scope
from tests.helpers import cli, latest_run

CONFIG = """\
# Why this goal: setup's reasoning, as comments.
goal: Make a checked change  # the title
observe: [{name: bench, command: "true"}]
agent:
  cli: mock
  mock:
    change_file: code.py
    change_content: "# iteration {iteration}"
"""


def commit(project, message="change"):
    git(project, "add", "-A")
    git(project, "commit", "-qm", message)


@pytest.fixture
def started(project):
    """The shared project with a commented config, and one finished run of it (landing
    manually: the tests commit config edits on the branch, which an auto-landing resume
    refuses as commits past the run's tip)."""
    (project / "autohelix.yaml").write_text(CONFIG)
    commit(project, "config")
    result = cli(project, "run", "-n", "1", "--no-report", "--land", "manual")
    assert result.exit_code == 0, result.output
    return latest_run(project)


# --- the stored copy ---


def test_a_config_without_template_is_stored_verbatim(started):
    assert started.config.read_text() == CONFIG


@pytest.mark.parametrize("template", ["no newline", "", "a\r\nb\r\n", "\n\n  x\n", "# h\n- k: 'v'\n"])
def test_a_config_with_a_template_is_stored_verbatim_and_its_template_beside_it(project, template):
    """Any template text, stored as its own file: nothing spliced into the config."""
    (project / "prompts").mkdir()
    (project / "prompts" / "p.md").write_bytes(template.encode())
    source = "# keep me\n" + CONFIG + "prompt_template: prompts/p.md  # a comment\n"
    (project / "autohelix.yaml").write_text(source)
    commit(project, "template")
    harness = Harness(project, new=True)
    paths = harness.paths
    harness.close()
    assert paths.config.read_text() == source
    assert paths.template.read_text() == (project / "prompts" / "p.md").read_text()  # the text the prompt reads
    assert stat.S_IMODE(paths.template.stat().st_mode) == 0o444
    config = load_run_config(paths)
    assert load_template(config) == (project / "prompts" / "p.md").read_text()
    assert config.prompt_template == (project / "prompts" / "p.md").resolve()
    assert config.protected == ["autohelix.yaml", "prompts/p.md"]


def test_a_config_without_template_stores_no_template(started):
    assert not started.template.exists()


# --- loading a stored copy ---


def test_the_stored_template_text_is_the_template(tmp_path):
    source = tmp_path / "autohelix.yaml"
    stored = tmp_path / "stored.yaml"
    stored.write_text("goal: g\nprompt_template: prompts/p.md\n")
    config = load_config(tmp_path, stored, source=source, template_text="Hi {{ goal }}")
    assert load_template(config) == "Hi {{ goal }}"
    assert config.prompt_template == (tmp_path / "prompts" / "p.md").resolve()  # next to the source
    assert not [i for i in config.validate() if i.level == "error"]  # the file needn't exist
    assert config.protected == ["autohelix.yaml", "prompts/p.md"]


def test_a_mapping_as_prompt_template_is_a_config_error(tmp_path):
    """The old stored form, `{file, text}` inlined into config.yaml, is no longer read."""
    (tmp_path / "c.yaml").write_text(yaml.safe_dump({"goal": "g", "prompt_template": {"file": "p.md", "text": "t"}}))
    config = load_config(tmp_path, tmp_path / "c.yaml")
    assert "prompt_template must be a path to a template file" in [
        i.message for i in config.validate() if i.level == "error"]


def test_a_run_whose_template_was_not_stored_refuses(project):
    (project / "prompt.md").write_text("Do it: {{ goal }}\n")
    (project / "autohelix.yaml").write_text(CONFIG + "prompt_template: prompt.md\n")
    commit(project, "template")
    assert cli(project, "run", "-n", "1", "--no-report").exit_code == 0
    paths = latest_run(project)
    paths.template.unlink()
    with pytest.raises(RuntimeError, match=rf"has no stored copy of its prompt_template: it was made by a "
                                           rf"development version .*\(`autohelix delete --force "
                                           rf"{short_id(paths.run_id)}`"):
        load_run_config(paths)


def test_protected_names_are_shown_plain_and_matched_escaped(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "p[1].md").write_text("t")
    (tmp_path / "outside.md").write_text("t")
    (project / "a.yaml").write_text("goal: g\nprompt_template: p[1].md\n")
    (project / "b.yaml").write_text("goal: g\nprompt_template: ../outside.md\n")
    config = load_config(project, project / "a.yaml")
    assert config.protected == config.frozen_names() == ["a.yaml", "p[1].md"]
    assert config.frozen_patterns() == ["a.yaml", "p[[]1].md"]
    assert not Scope(frozen=tuple(config.frozen_patterns())).allows("p[1].md")
    assert Scope(frozen=tuple(config.frozen_patterns())).allows("p1.md")
    config = load_config(project, project / "b.yaml")
    assert config.protected == ["b.yaml"]
    assert config.frozen_patterns() == ["b.yaml"] and config.frozen == []


def test_a_symlinked_config_and_template_are_frozen_as_named_and_as_resolved(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "c.yaml").write_text("goal: g\nprompt_template: ../p.md\n")
    (tmp_path / "real" / "t.md").write_text("t")
    (tmp_path / "autohelix.yaml").symlink_to("real/c.yaml")
    (tmp_path / "p.md").symlink_to("real/t.md")
    config = load_config(tmp_path, tmp_path / "autohelix.yaml")
    # The template's path resolves next to the config's target (real/../p.md), itself a link.
    assert config.protected == ["autohelix.yaml", "real/c.yaml", "p.md", "real/t.md"]
    scope = Scope(frozen=tuple(config.frozen_patterns()))
    assert not any(scope.allows(name) for name in config.protected)


# --- resume compares, refuses before spending ---


def test_a_comment_or_formatting_edit_resumes(started):
    (started.project / "autohelix.yaml").write_text(
        "# reworded reasoning\n" + CONFIG.replace('observe: [{name: bench, command: "true"}]',
                                                "observe:   [ {command: 'true',  name: bench} ]   # still there"))
    commit(started.project)
    result = cli(started.project, "resume", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert History(started.project, started.run_id).get_last_iteration() == 2
    assert started.config.read_text() == CONFIG  # the stored copy never changes


def refused(paths, result, keys, unlanded):
    short = short_id(paths.run_id)
    text = " ".join(result.output.split())
    assert result.exit_code == 1, result.output
    said = (f"autohelix.yaml changed since run {short} started (changed: {keys}); the run keeps the config it "
            f"started with. To apply the new config, start a new run that continues from this one: "
            f"`autohelix run --import {short}`. ")
    assert said in text and text.endswith("To continue this run, restore the file."), text
    between = text[text.index(said) + len(said):].removesuffix("To continue this run, restore the file.")
    # Its unlanded commits (`unlanded`: manual landing, and the config's commit made main
    # diverge): land them first, to start the new run from them.
    assert between == (f"{short}'s 1 accepted commit isn't in main: land it first (`git merge "
                       f"autohelix/{paths.run_id}`, or `git switch -c <branch> autohelix/{paths.run_id}`) to start "
                       "from it. " if unlanded else ""), between
    assert History(paths.project, paths.run_id).get_last_iteration() == 1  # nothing spent


def test_a_value_edit_refuses(started):
    (started.project / "autohelix.yaml").write_text(CONFIG.replace("Make a checked change", "Something else"))
    commit(started.project)
    refused(started, cli(started.project, "resume", "-n", "1", "--no-report"), "goal", unlanded=True)
    refused(started, cli(started.project, "resume", short_id(started.run_id), "--no-report"), "goal", unlanded=True)


def test_a_template_edit_refuses(project):
    (project / "prompt.md").write_text("Do it: {{ goal }}\n")
    (project / "autohelix.yaml").write_text(CONFIG + "prompt_template: prompt.md\n")
    commit(project, "template")
    assert cli(project, "run", "-n", "1", "--no-report").exit_code == 0
    paths = latest_run(project)
    assert paths.template.read_text() == "Do it: {{ goal }}\n"
    assert "Do it: Make a checked change" in paths.prompt(1).read_text()
    (project / "prompt.md").write_text("Do it differently: {{ goal }}\n")
    refused(paths, cli(project, "resume", "-n", "1", "--no-report"), "prompt_template", unlanded=False)
    (project / "prompt.md").unlink()
    (project / "prompt.md").mkdir()  # there, but unreadable: a change too
    result = cli(project, "resume", "-n", "1", "--no-report")
    assert result.exit_code == 1 and "it no longer reads: Cannot read prompt_template " in result.output
    (project / "prompt.md").rmdir()
    (project / "prompt.md").write_text("Do it: {{ goal }}\n")  # restored
    assert cli(project, "resume", "-n", "1", "--no-report").exit_code == 0


def test_a_deleted_template_continues_but_other_keys_are_compared(project):
    (project / "prompt.md").write_text("Do it: {{ goal }}\n")
    (project / "autohelix.yaml").write_text(CONFIG + "prompt_template: prompt.md\n")
    commit(project, "template")
    assert cli(project, "run", "-n", "1", "--no-report", "--land", "manual").exit_code == 0
    paths = latest_run(project)
    git(project, "rm", "-q", "prompt.md")
    commit(project, "drop template")
    (project / "autohelix.yaml").write_text(CONFIG.replace("Make a checked change", "Other") + "prompt_template: prompt.md\n")
    refused(paths, cli(project, "resume", "-n", "1", "--no-report"), "goal", unlanded=True)
    (project / "autohelix.yaml").write_text(CONFIG + "prompt_template: prompt.md\n")
    result = cli(project, "resume", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output
    assert (f"prompt.md is gone; run {short_id(paths.run_id)} continues with the prompt template it stored "
            "when it started.") in " ".join(result.output.split())
    assert "Do it: Make a checked change" in paths.prompt(2).read_text()


@pytest.mark.parametrize("edit", [
    "gates: []\nscope: {editable: null}\nstop:\n",
    "prompt_template: ./prompt.md\n",
    # a default written out, another spelling of the same value: the harness sees the same config
    'observe: [{name: bench, command: "true", timeout: 1800}]\n',
    'observe: [{name: bench, command: "true", timeout: 30m}]\n',
])
def test_other_spellings_of_the_same_config_resume(project, edit):
    (project / "prompt.md").write_text("Do it: {{ goal }}\n")
    (project / "autohelix.yaml").write_text(CONFIG + "prompt_template: prompt.md\n")
    commit(project, "template")
    assert cli(project, "run", "-n", "1", "--no-report").exit_code == 0
    text = CONFIG + "prompt_template: prompt.md\n"
    if edit.startswith("prompt_template"):
        text = text.replace("prompt_template: prompt.md\n", edit)
    elif edit.startswith("observe"):
        text = text.replace('observe: [{name: bench, command: "true"}]\n', edit)
    else:
        text += edit
    (project / "autohelix.yaml").write_text(text)
    result = cli(project, "resume", "-n", "1", "--no-report")
    assert result.exit_code == 0, result.output


def parsed(tmp_path, text, template=None):
    (tmp_path / "c.yaml").write_text(text)
    if template is not None:
        (tmp_path / "p.md").write_text(template)
    return load_config(tmp_path, tmp_path / "c.yaml")


def test_config_changes_compares_what_the_harness_uses(tmp_path):
    """Parsed configs: the old raw-YAML comparison counted each of these as a change."""
    def changes(a, b):
        return config_changes(parsed(tmp_path, a), parsed(tmp_path, b))

    assert changes("goal: g\ngates: [pytest]\n", "goal: g\ngates: [{command: pytest, timeout: 1800}]\n") == []
    assert changes("goal: g\nagent: {timeout: 10m}\n", "goal: g\nagent: {timeout: 600, cli: claude-code}\n") == []
    assert changes("goal: g\nscope: {editable: null}\ngates: []\n", "goal: g\n") == []
    assert changes("goal: g\nagent: {timeout: 10m}\n", "goal: g\nagent: {timeout: 11m}\n") == ["agent"]
    assert changes("goal: g\nscope: {editable: [src/]}\n", "goal: h\nscope: {editable: [./src/]}\n") == ["goal"]
    assert changes("goal: g\nscope: {frozen: [a]}\n", "goal: g\nscope: {frozen: [b]}\n") == ["scope.frozen"]


def test_config_changes_compares_the_template_texts(tmp_path):
    stored = parsed(tmp_path, "goal: g\nprompt_template: p.md\n", "one")
    stored.prompt_template_text = "one"  # as a run's stored copy has it
    assert config_changes(stored, parsed(tmp_path, "goal: g\nprompt_template: ./p.md\n", "one")) == []
    assert config_changes(stored, parsed(tmp_path, "goal: g\nprompt_template: p.md\n", "two")) == ["prompt_template"]
    assert config_changes(stored, parsed(tmp_path, "goal: g\n")) == ["prompt_template"]


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

    real = runs.template_text

    def store_then_edit(config):  # called right after the config's text is read
        (project / "autohelix.yaml").write_text(CONFIG.replace("Make a checked change", "Edited meanwhile"))
        return real(config)

    monkeypatch.setattr(runs, "template_text", store_then_edit)
    harness = Harness(project, new=True)
    assert harness.config.goal == "Make a checked change"  # its stored copy
    harness.close()


def test_a_run_without_a_stored_config_refuses(started):
    started.config.unlink()
    with pytest.raises(RuntimeError, match=r"has no stored config: it was made by a development version .*\("
                                           rf"`autohelix delete --force {short_id(started.run_id)}`"):
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
