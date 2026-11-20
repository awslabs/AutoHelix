"""docs/patterns.md stays true to the config parser and the CLI: every config in it
loads and validates, every variant fragment does too once merged into its pattern's
config, and every launch command's flags parse as `autohelix run`'s."""

import re
import shlex
from pathlib import Path

import pytest
import yaml

from autohelix import cli
from autohelix.config import load_config

PATTERNS = Path(__file__).resolve().parent.parent / "docs" / "patterns.md"


def sections() -> list[tuple[str, str]]:
    """(heading, text) for each `## ` section."""
    parts = re.split(r"^## (.+)$", PATTERNS.read_text(), flags=re.M)
    return list(zip(parts[1::2], parts[2::2]))


def yaml_blocks(text: str) -> list[dict]:
    return [yaml.safe_load(block) for block in re.findall(r"^ *```yaml\n(.*?)^ *```", text, re.M | re.S)]


def merged(base: dict, fragment: dict) -> dict:
    """A variant on its pattern's config: a fragment's list replaces the base's (a
    variant shows the whole list it means), other keys are set."""
    return {**base, **fragment}


def configs() -> list[tuple[str, dict]]:
    found = []
    for heading, text in sections():
        base = None
        for block in yaml_blocks(text):
            if "goal" in block:
                base = block
                found.append((heading, block))
            else:
                assert base is not None, f"{heading}: a fragment before any config"
                found.append((f"{heading} (variant)", merged(base, block)))
    return found


def launches() -> list[str]:
    return re.findall(r"Launch: `(autohelix run [^`]+)`", PATTERNS.read_text())


def test_every_pattern_has_a_config_and_a_launch():
    names = [heading for heading, _ in configs() if "variant" not in heading]
    assert {"A checked task", "Optimization", "Training a model", "Working through a spec",
            "Writing and research"} <= set(names)
    assert len(launches()) == 6


def test_each_config_file_is_an_experiment_folder_its_launch_names():
    """Every config is shown at `autohelix/NAME/config.yaml`, and the launch
    commands name exactly those folders."""
    text = PATTERNS.read_text()
    folders = set(re.findall(r"^`autohelix/([a-z0-9-]+)/autohelix\.yaml`:$", text, re.M))
    assert not re.search(r"autohelix/[a-z0-9-]+\.yaml", text)  # not a config any more
    assert folders == {shlex.split(command)[2] for command in launches()}


@pytest.mark.parametrize("heading, config", configs(), ids=[h for h, _ in configs()])
def test_the_configs_validate(heading, config, tmp_path):
    path = tmp_path / "autohelix.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    loaded = load_config(tmp_path, config_file=path)
    assert [issue.message for issue in loaded.validate() if issue.level == "error"] == []


@pytest.mark.parametrize("command", launches())
def test_the_launch_commands_parse(command, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = shlex.split(command)[2:]  # after `autohelix run`
    context = cli.run.make_context("run", args)  # parses and checks the flags, runs nothing
    assert context.params["config_name"] in ("fix", "perf", "train", "tasks", "guide")
