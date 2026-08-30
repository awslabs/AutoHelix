"""Parse release-note entries."""


def parse_entry(entry: str) -> tuple[str, str]:
    """Return the baseline category and unparsed entry."""
    return "other", entry.strip()
