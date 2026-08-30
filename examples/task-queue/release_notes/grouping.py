"""Group release-note entries."""


def group_entries(entries: list[str]) -> dict[str, list[str]]:
    """Return the baseline single-group representation."""
    return {"other": list(entries)}
