"""Render release notes."""


def render_release_notes(entries: list[str]) -> str:
    """Render the baseline flat bullet list."""
    return "\n".join(f"- {entry}" for entry in entries)
