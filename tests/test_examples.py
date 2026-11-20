"""The bundled examples' configs: each example is one experiment,
autohelix/<example>/config.yaml, which a bare `autohelix run` finds, in the example and
in the project `examples/setup_example.py` makes of it."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

from autohelix.config import experiment_config, find_config, load_config, project_configs

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
# Scaffolded by their own setup.py from templates/config.yaml.tmpl, no config in the folder.
SCAFFOLDED = {"algotune", "kernelbench"}
BUNDLED = sorted(d.name for d in EXAMPLES.iterdir() if d.is_dir() and d.name not in SCAFFOLDED
                 and not d.name.startswith((".", "__")))


def _setup_example():
    spec = importlib.util.spec_from_file_location("setup_example", EXAMPLES / "setup_example.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("example", BUNDLED)
def test_every_bundled_example_is_one_experiment_named_after_it(example):
    folder = EXAMPLES / example
    assert project_configs(folder) == [experiment_config(example)]
    assert find_config(folder) == folder / experiment_config(example)
    assert load_config(folder).validate() == []


@pytest.mark.parametrize("example", sorted(SCAFFOLDED))
def test_scaffolded_examples_ship_a_config_template_not_a_config(example):
    folder = EXAMPLES / example
    assert project_configs(folder) == []
    assert (folder / "templates" / "config.yaml.tmpl").is_file()


def test_setup_example_lists_and_sets_up_the_bundled_examples(tmp_path):
    setup = _setup_example()
    assert setup.list_bundled() == [name for name in BUNDLED if name not in setup.REFERENCE_EXAMPLES]
    project = tmp_path / "sorting"
    assert setup.setup_bundled("sorting", project)
    assert find_config(project) == project / "autohelix" / "sorting" / "config.yaml"
    committed = subprocess.run(["git", "ls-files"], cwd=project, capture_output=True, text=True,
                               check=True).stdout.split()
    assert "autohelix/sorting/config.yaml" in committed
