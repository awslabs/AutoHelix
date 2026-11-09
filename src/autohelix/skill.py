# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Export the packaged setup skill to an agent's discovery directory."""

import os
import stat
import tempfile
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

import click

SKILL_NAME = "autohelix-setup"
PROJECT_LOCATIONS = {
    "claude": Path(".claude/skills"),
    "codex": Path(".agents/skills"),
    "opencode": Path(".opencode/skills"),
}
USER_LOCATIONS = {
    "claude": Path(".claude/skills"),
    "codex": Path(".agents/skills"),
    "opencode": Path(".config/opencode/skills"),
}


def _bundled_files() -> dict[Path, bytes]:
    """Read resources through Traversable so an installed wheel is sufficient."""
    root = files("autohelix").joinpath("skills", SKILL_NAME)

    def walk(directory: Traversable, prefix: Path = Path()):
        for resource in sorted(directory.iterdir(), key=lambda entry: entry.name):
            relative = prefix / resource.name
            if resource.is_dir():
                yield from walk(resource, relative)
            else:
                yield relative, resource.read_bytes()

    return dict(walk(root))


def export_skill(destination: Path, *, force: bool = False) -> None:
    """Copy the complete skill, preserving unrelated files on explicit updates."""
    try:
        if destination.is_symlink():
            raise click.ClickException(f"Skill destination is a symlink: {destination}")
        if destination.exists() and not force:
            raise click.ClickException(
                f"Skill destination already exists: {destination}\n"
                "Use --force to overwrite bundled files, or export to another directory."
            )
        if destination.exists() and not destination.is_dir():
            raise click.ClickException(f"Skill destination is not a directory: {destination}")

        resources = _bundled_files()
        # Check all existing bundled paths before writing any of them.
        for relative in resources:
            target = destination / relative
            for parent in [target, *target.parents]:
                if parent == destination:
                    break
                if parent.is_symlink():
                    raise click.ClickException(f"Bundled skill path is a symlink: {parent}")
                if parent.exists() and (
                    not parent.is_file() if parent == target else not parent.is_dir()
                ):
                    raise click.ClickException(
                        f"Bundled skill path has an incompatible type: {parent}"
                    )

        for relative, content in resources.items():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            # Replace files instead of truncating them: a hard link to a bundled
            # file must not cause an update to change an unrelated local file.
            mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
                    temporary = Path(output.name)
                    output.write(content)
                temporary.chmod(mode)
                os.replace(temporary, target)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
    except OSError as exc:
        raise click.ClickException(f"Could not write skill to {destination}: {exc}") from exc


@click.group()
def skill():
    """Install or export the AutoHelix setup skill."""


@skill.command("install")
@click.option(
    "--agent", type=click.Choice(list(PROJECT_LOCATIONS)), required=True,
    help="Agent that will discover the skill.",
)
@click.option(
    "--scope", type=click.Choice(["project", "user"]), default="project",
    show_default=True, help="Install for one project or your user account.",
)
@click.option(
    "--path", "-p", type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Project directory (default: current directory); only for --scope project.",
)
@click.option("--force", is_flag=True, help="Overwrite bundled files in an existing skill.")
def install(agent: str, scope: str, path: Path | None, force: bool):
    """Install the skill and its references in an agent discovery directory."""
    if scope == "project":
        destination = (path or Path.cwd()).resolve() / PROJECT_LOCATIONS[agent] / SKILL_NAME
    else:
        if path is not None:
            raise click.UsageError("--path is only supported with --scope project.")
        destination = Path.home() / USER_LOCATIONS[agent] / SKILL_NAME
    export_skill(destination, force=force)
    click.echo(f"Installed AutoHelix setup skill at {destination}")
    click.echo("Ask your agent to use autohelix-setup to prepare an improvement run.")


@skill.command("export")
@click.argument("destination", type=click.Path(path_type=Path))
@click.option("--force", is_flag=True, help="Overwrite bundled files in an existing skill.")
def export(destination: Path, force: bool):
    """Copy the skill into DESTINATION, including SKILL.md and references."""
    target = destination.absolute()
    export_skill(target, force=force)
    click.echo(f"Exported AutoHelix setup skill to {target}")
