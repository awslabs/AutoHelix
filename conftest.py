"""Fixtures for every collected test (tests/ and examples/)."""

import pytest

from autohelix.runs import COPIES_ENV


@pytest.fixture(autouse=True)
def copies_dir(tmp_path_factory, monkeypatch):
    """Candidate copies go to a private folder beside the test's project, never the real cache."""
    base = tmp_path_factory.mktemp("copies").resolve()
    monkeypatch.setenv(COPIES_ENV, str(base))
    monkeypatch.delenv("GIT_CEILING_DIRECTORIES", raising=False)  # runs prepend to it
    return base
