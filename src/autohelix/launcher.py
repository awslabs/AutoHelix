# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""The `autohelix` executable for commands the harness runs.

Gate, observe and stop commands run with the project's sources remapped onto
the candidate's copy (`sandbox.build_worktree_env`), so a plain `autohelix`
there would import candidate code first (a planted `yaml.py` or `click.py`
could print `##autohelix[missing=0]` from inside `autohelix review`). The
harness puts a launcher first on those commands' PATH instead: it runs the
harness's own interpreter in isolated mode (`-I`: no PYTHONPATH, no user site,
not the current directory) with the harness's own `autohelix` package first on
sys.path, so it imports the same AutoHelix the harness runs, whatever the
command's environment and working directory.
"""

import os
import shlex
import sys
from pathlib import Path

import autohelix

NAME = "autohelix"


def write_launcher(directory: Path) -> Path:
    """Write `<directory>/autohelix` (mode 0500); returns the directory."""
    directory = Path(directory)
    directory.mkdir(mode=0o700, exist_ok=True)
    package_parent = str(Path(autohelix.__file__).resolve().parent.parent)
    code = (f"import sys; sys.path.insert(0, {package_parent!r}); "
            f"from autohelix.cli import main; main(prog_name={NAME!r})")
    # The interpreter as the harness was started with it (a venv's python must stay the
    # venv's, so not resolved).
    script = f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I -c {shlex.quote(code)} "$@"\n'
    path = directory / NAME
    if os.path.lexists(path):
        path.unlink()
    path.write_text(script)
    path.chmod(0o500)
    return directory


def command_path(directory: Path) -> str:
    """$PATH for a command: the launcher's directory first."""
    return f"{directory}{os.pathsep}{os.environ.get('PATH', os.defpath)}"
