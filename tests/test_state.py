"""Tests for autohelix.state — persistent .autohelix/ state helpers."""

from autohelix.state import (
    clear_stored_config_path,
    ensure_gitignore_entry,
    read_stored_config_path,
    read_stored_config_rel,
    store_config_path,
)


class TestEnsureGitignoreEntry:
    def test_creates_file_when_absent(self, tmp_path):
        ensure_gitignore_entry(tmp_path, ".autohelix/")
        assert (tmp_path / ".gitignore").read_text() == ".autohelix/\n"

    def test_appends_when_missing(self, tmp_path):
        (tmp_path / ".gitignore").write_text("node_modules/\n")
        ensure_gitignore_entry(tmp_path, ".autohelix/")
        assert (tmp_path / ".gitignore").read_text() == "node_modules/\n.autohelix/\n"

    def test_idempotent_when_present(self, tmp_path):
        (tmp_path / ".gitignore").write_text(".autohelix/\n")
        ensure_gitignore_entry(tmp_path, ".autohelix/")
        assert (tmp_path / ".gitignore").read_text() == ".autohelix/\n"

    def test_adds_newline_before_append_when_missing(self, tmp_path):
        (tmp_path / ".gitignore").write_text("node_modules/")  # no trailing newline
        ensure_gitignore_entry(tmp_path, ".autohelix/")
        assert (tmp_path / ".gitignore").read_text() == "node_modules/\n.autohelix/\n"


class TestStoredConfigPath:
    def test_read_returns_none_when_unset(self, tmp_path):
        assert read_stored_config_rel(tmp_path) is None
        assert read_stored_config_path(tmp_path) is None

    def test_store_then_read_rel(self, tmp_path):
        store_config_path(tmp_path, "configs/fast.yaml")
        assert read_stored_config_rel(tmp_path) == "configs/fast.yaml"

    def test_read_path_resolves_when_file_exists(self, tmp_path):
        (tmp_path / "autohelix.yaml").write_text("goal: x\n")
        store_config_path(tmp_path, "autohelix.yaml")
        resolved = read_stored_config_path(tmp_path)
        assert resolved == tmp_path / "autohelix.yaml"

    def test_rel_is_existence_agnostic_but_path_is_not(self, tmp_path):
        # The config file was recorded but no longer exists on disk.
        store_config_path(tmp_path, "gone.yaml")
        # rel still reports it (used to detect a config switch between runs)...
        assert read_stored_config_rel(tmp_path) == "gone.yaml"
        # ...but the resolving variant returns None (report needs a real file).
        assert read_stored_config_path(tmp_path) is None

    def test_clear_forgets_stored_path(self, tmp_path):
        store_config_path(tmp_path, "autohelix.yaml")
        clear_stored_config_path(tmp_path)
        assert read_stored_config_rel(tmp_path) is None

    def test_clear_is_safe_when_unset(self, tmp_path):
        clear_stored_config_path(tmp_path)  # no error
        assert read_stored_config_rel(tmp_path) is None
