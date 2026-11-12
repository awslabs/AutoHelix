"""Portable setup skill resources and explicit CLI installation."""

import os
import re
import zipfile
from importlib.resources import files
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from autohelix.cli import main
from autohelix.config import Config


SKILL_NAME = "autohelix-setup"
RESOURCE_PATHS = [
    "SKILL.md",
    "references/config.md",
    "references/loop-design.md",
    "references/examples.md",
    "references/managed-runs.md",
]
PROJECT_DIRS = {
    "claude": ".claude/skills",
    "codex": ".agents/skills",
    "opencode": ".opencode/skills",
}
USER_DIRS = {
    "claude": ".claude/skills",
    "codex": ".agents/skills",
    "opencode": ".config/opencode/skills",
}


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def resources():
    root = files("autohelix").joinpath("skills", SKILL_NAME)
    return {name: root.joinpath(*name.split("/")).read_bytes() for name in RESOURCE_PATHS}


@pytest.fixture(autouse=True)
def temporary_home(tmp_path, monkeypatch):
    """User-scope tests must never write to the real account."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def snapshot(directory):
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def test_export_full_folder(runner, resources, tmp_path):
    destination = tmp_path / "nested" / SKILL_NAME
    result = runner.invoke(main, ["skill", "export", str(destination)])
    assert result.exit_code == 0, result.output
    assert snapshot(destination) == resources


@pytest.mark.parametrize("agent", PROJECT_DIRS)
def test_project_install(runner, resources, tmp_path, agent):
    project = tmp_path / "project"
    project.mkdir()
    result = runner.invoke(
        main, ["skill", "install", "--agent", agent, "--scope", "project", "-p", str(project)]
    )
    assert result.exit_code == 0, result.output
    assert snapshot(project / PROJECT_DIRS[agent] / SKILL_NAME) == resources
    assert not (project / ".autohelix").exists()
    assert not (project / "autohelix.yaml").exists()


@pytest.mark.parametrize("agent", USER_DIRS)
def test_user_install(runner, resources, temporary_home, agent):
    result = runner.invoke(main, ["skill", "install", "--agent", agent, "--scope", "user"])
    assert result.exit_code == 0, result.output
    assert snapshot(temporary_home / USER_DIRS[agent] / SKILL_NAME) == resources


def test_project_defaults_to_cwd(runner, resources, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(main, ["skill", "install", "--agent", "codex"])
    assert result.exit_code == 0, result.output
    assert snapshot(tmp_path / PROJECT_DIRS["codex"] / SKILL_NAME) == resources


@pytest.mark.parametrize("existing", ["empty", "customized", "file"])
def test_export_preserves_existing_destination(runner, tmp_path, existing):
    destination = tmp_path / "skill"
    if existing == "file":
        destination.write_bytes(b"local file")
    else:
        destination.mkdir()
        if existing == "customized":
            (destination / "SKILL.md").write_bytes(b"local instructions")
            (destination / "custom.md").write_bytes(b"local reference")
    before = destination.read_bytes() if existing == "file" else snapshot(destination)
    result = runner.invoke(main, ["skill", "export", str(destination)])
    assert result.exit_code == 1, result.output
    assert "already exists" in result.output
    after = destination.read_bytes() if existing == "file" else snapshot(destination)
    assert after == before


@pytest.mark.parametrize("command", ["export", "install"])
def test_force_updates_bundled_files_and_preserves_local_files(
    runner, resources, tmp_path, command
):
    if command == "export":
        destination = tmp_path / "skill"
        args = ["skill", "export", str(destination)]
    else:
        destination = tmp_path / PROJECT_DIRS["codex"] / SKILL_NAME
        args = ["skill", "install", "--agent", "codex", "--path", str(tmp_path)]
    result = runner.invoke(main, args)
    assert result.exit_code == 0, result.output
    (destination / "SKILL.md").write_bytes(b"old instructions")
    (destination / "references/config.md").write_bytes(b"old config")
    (destination / "references/local.md").write_bytes(b"local reference")
    (destination / "other").mkdir()
    (destination / "other/local.txt").write_bytes(b"local notes")
    before = snapshot(destination)

    result = runner.invoke(main, args)
    assert result.exit_code == 1, result.output
    assert snapshot(destination) == before

    result = runner.invoke(main, [*args, "--force"])
    assert result.exit_code == 0, result.output
    assert snapshot(destination) == {
        **resources,
        "references/local.md": b"local reference",
        "other/local.txt": b"local notes",
    }


def test_force_preserves_unrelated_hard_links(runner, resources, tmp_path):
    destination = tmp_path / "skill"
    destination.mkdir()
    local = tmp_path / "local.md"
    local.write_bytes(b"local instructions")
    os.link(local, destination / "SKILL.md")
    result = runner.invoke(main, ["skill", "export", str(destination), "--force"])
    assert result.exit_code == 0, result.output
    assert local.read_bytes() == b"local instructions"
    assert snapshot(destination) == resources


@pytest.mark.parametrize(
    "conflict",
    ["destination-file", "skill-directory", "references-file", "reference-directory"],
)
def test_force_rejects_type_conflicts_before_writing(runner, tmp_path, conflict):
    destination = tmp_path / "skill"
    if conflict == "destination-file":
        destination.write_bytes(b"local file")
    else:
        destination.mkdir()
        (destination / "SKILL.md").write_bytes(b"old instructions")
        if conflict == "skill-directory":
            (destination / "SKILL.md").unlink()
            (destination / "SKILL.md").mkdir()
        elif conflict == "references-file":
            (destination / "references").write_bytes(b"local file")
        else:
            (destination / "references/config.md").mkdir(parents=True)
    before = snapshot(tmp_path)
    result = runner.invoke(main, ["skill", "export", str(destination), "--force"])
    assert result.exit_code == 1, result.output
    assert "type" in result.output or "not a directory" in result.output
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("link_path", [".", "SKILL.md", "references", "references/config.md"])
@pytest.mark.parametrize("dangling", [False, True])
def test_force_rejects_symlinks_before_writing(runner, tmp_path, link_path, dangling):
    destination = tmp_path / "skill"
    destination.mkdir()
    (destination / "SKILL.md").write_bytes(b"old instructions")
    outside = tmp_path / "outside"
    if not dangling:
        outside.mkdir()
        (outside / "keep.md").write_bytes(b"local reference")
    link = destination / link_path
    if link_path == ".":
        (destination / "SKILL.md").unlink()
        destination.rmdir()
    elif link_path == "SKILL.md":
        link.unlink()
    else:
        link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    before = snapshot(tmp_path)
    result = runner.invoke(main, ["skill", "export", str(destination), "--force"])
    assert result.exit_code == 1, result.output
    assert "symlink" in result.output
    assert link.is_symlink()
    assert snapshot(tmp_path) == before


def test_export_from_zip_resources(runner, resources, tmp_path, monkeypatch):
    """Resource access must work without treating package data as filesystem paths."""
    archive = tmp_path / "resources.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, content in resources.items():
            bundle.writestr(f"autohelix/skills/{SKILL_NAME}/{name}", content)
    with zipfile.ZipFile(archive) as bundle:
        monkeypatch.setattr("autohelix.skill.files", lambda package: zipfile.Path(bundle, "autohelix/"))
        destination = tmp_path / "export"
        result = runner.invoke(main, ["skill", "export", str(destination)])
        assert result.exit_code == 0, result.output
        assert snapshot(destination) == resources


def test_io_error_is_actionable(runner, tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr("autohelix.skill.os.replace", denied)
    destination = tmp_path / "export"
    result = runner.invoke(main, ["skill", "export", str(destination)])
    assert result.exit_code == 1, result.output
    assert "Could not write skill" in result.output
    assert "permission denied" in result.output
    assert snapshot(destination) == {}  # Temporary file is cleaned up.


@pytest.mark.parametrize(
    "args",
    [
        ["install"],
        ["install", "--agent", "other"],
        ["install", "--agent", "codex", "--scope", "other"],
        ["export"],
    ],
)
def test_invalid_arguments(runner, args):
    result = runner.invoke(main, ["skill", *args])
    assert result.exit_code == 2, result.output


def test_user_scope_rejects_project_path(runner, tmp_path, temporary_home):
    result = runner.invoke(
        main,
        ["skill", "install", "--agent", "codex", "--scope", "user", "--path", str(tmp_path)],
    )
    assert result.exit_code == 2, result.output
    assert "--path is only supported" in result.output
    assert snapshot(temporary_home) == {}


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_invalid_project_path(runner, tmp_path, kind):
    project = tmp_path / kind
    if kind == "file":
        project.write_bytes(b"local file")
    result = runner.invoke(main, ["skill", "install", "--agent", "codex", "--path", str(project)])
    assert result.exit_code == 2, result.output
    assert not (tmp_path / PROJECT_DIRS["codex"]).exists()


def test_portable_skill_frontmatter_and_reference_links(resources):
    text = resources["SKILL.md"].decode()
    frontmatter, body = text.removeprefix("---\n").split("\n---\n", 1)
    metadata = yaml.safe_load(frontmatter)
    assert metadata["name"] == SKILL_NAME
    assert isinstance(metadata["description"], str) and metadata["description"].strip()
    linked = set(re.findall(r"\]\((references/[^)]+)\)", body))
    assert linked == set(RESOURCE_PATHS) - {"SKILL.md"}
    for name, content in resources.items():
        for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", content.decode()):
            relative = (Path(name).parent / target).as_posix()
            assert relative in resources, f"Missing portable reference: {relative}"


# The skill's documents still use the first config format; they move to config v2 with the docs.
@pytest.mark.xfail(strict=True, reason="skill docs not yet on config v2")
@pytest.mark.parametrize("name", ["references/config.md", "references/examples.md"])
def test_documented_yaml_validates(resources, name):
    snippets = re.findall(r"```yaml\n(.*?)```", resources[name].decode(), re.DOTALL)
    assert snippets
    for snippet in snippets:
        raw = yaml.safe_load(snippet)
        # The agent/reviewer excerpt is a config fragment, not a whole run.
        data = {
            "goal": "Improve the document while preserving factual accuracy.",
            "gates": ["python validate_guide.py"],
            **raw,
        }
        assert Config.from_dict(data).validate(data) == []
