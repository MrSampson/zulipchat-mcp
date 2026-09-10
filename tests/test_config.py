import pytest

from zulipchat_mcp.config import ConfigManager, DatabaseBackend


def test_database_config_defaults_to_duckdb(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_BACKEND", raising=False)
    monkeypatch.delenv("ZULIPCHAT_DB_PATH", raising=False)

    config = ConfigManager()

    assert config.config.database.backend is DatabaseBackend.DUCKDB
    assert config.config.database.path == ".mcp/zulipchat/zulipchat.duckdb"


def test_database_config_reads_sqlite_backend_with_default_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.delenv("ZULIPCHAT_DB_PATH", raising=False)

    config = ConfigManager()

    assert config.config.database.backend is DatabaseBackend.SQLITE
    assert config.config.database.path == ".mcp/zulipchat/zulipchat.sqlite3"


def test_database_config_reads_explicit_path_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "duckdb")
    monkeypatch.setenv("ZULIPCHAT_DB_PATH", "/tmp/custom.duckdb")

    config = ConfigManager()

    assert config.config.database.path == "/tmp/custom.duckdb"


def test_database_config_reads_postgres_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "postgres")
    monkeypatch.setenv("POSTGRES_HOST", "db.internal")
    monkeypatch.setenv("POSTGRES_PORT", "6543")
    monkeypatch.setenv("POSTGRES_DB", "zulipchat")
    monkeypatch.setenv("POSTGRES_USER", "mcp")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")

    config = ConfigManager()

    db = config.config.database
    assert db.backend is DatabaseBackend.POSTGRES
    assert db.postgres_host == "db.internal"
    assert db.postgres_port == 6543
    assert db.postgres_db == "zulipchat"
    assert db.postgres_user == "mcp"
    assert db.postgres_password == "secret"


def test_database_config_rejects_unknown_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "mongodb")

    with pytest.raises(ValueError, match="DATABASE_BACKEND"):
        ConfigManager()


def test_database_config_postgres_port_defaults_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_BACKEND", "postgres")
    monkeypatch.delenv("POSTGRES_PORT", raising=False)

    config = ConfigManager()

    assert config.config.database.postgres_port == 5432
