"""Settings that matter once the API is on a public hostname."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from simcore.config import Settings, get_settings, normalize_database_url


def test_postgres_scheme_is_rewritten_to_psycopg() -> None:
    assert (
        normalize_database_url("postgres://simcore:secret@db.internal:5432/simcore")
        == "postgresql+psycopg://simcore:secret@db.internal:5432/simcore"
    )
    assert (
        normalize_database_url("postgresql://simcore:secret@db.internal:5432/simcore")
        == "postgresql+psycopg://simcore:secret@db.internal:5432/simcore"
    )
    assert (
        normalize_database_url("postgresql+psycopg://simcore:secret@db.internal:5432/simcore")
        == "postgresql+psycopg://simcore:secret@db.internal:5432/simcore"
    )


def test_settings_normalize_database_url_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIMCORE_DATABASE_URL", "postgres://simcore:secret@127.0.0.1:5432/simcore")
    get_settings.cache_clear()
    try:
        settings = Settings(_env_file=None)
        assert settings.database_url == "postgresql+psycopg://simcore:secret@127.0.0.1:5432/simcore"
    finally:
        get_settings.cache_clear()


def test_production_rejects_the_dev_admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIMCORE_ENV", "production")
    monkeypatch.setenv("SIMCORE_ADMIN_TOKEN", "dev-admin")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError, match="SIMCORE_ADMIN_TOKEN"):
            Settings(_env_file=None)
    finally:
        get_settings.cache_clear()


def test_production_rejects_a_blank_admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIMCORE_ENV", "production")
    monkeypatch.setenv("SIMCORE_ADMIN_TOKEN", "   ")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError, match="SIMCORE_ADMIN_TOKEN"):
            Settings(_env_file=None)
    finally:
        get_settings.cache_clear()


def test_production_accepts_a_real_admin_token_and_keeps_admin_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIMCORE_ENV", "production")
    monkeypatch.setenv("SIMCORE_ADMIN_TOKEN", "generated-token-not-a-default")
    monkeypatch.setenv("SIMCORE_PLAYER_TOKEN_SECRET", "p" * 48)
    monkeypatch.delenv("SIMCORE_ENABLE_ADMIN", raising=False)
    get_settings.cache_clear()
    try:
        settings = Settings(_env_file=None)
        assert settings.admin_token == "generated-token-not-a-default"
        assert settings.admin_enabled is False
        assert settings.embedded_worker is False
    finally:
        get_settings.cache_clear()


def test_production_rejects_a_missing_or_dev_player_token_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIMCORE_ENV", "production")
    monkeypatch.setenv("SIMCORE_ADMIN_TOKEN", "generated-token-not-a-default")
    monkeypatch.delenv("SIMCORE_PLAYER_TOKEN_SECRET", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError, match="SIMCORE_PLAYER_TOKEN_SECRET"):
            Settings(_env_file=None)
    finally:
        get_settings.cache_clear()


def test_production_rejects_a_short_player_token_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIMCORE_ENV", "production")
    monkeypatch.setenv("SIMCORE_ADMIN_TOKEN", "generated-token-not-a-default")
    monkeypatch.setenv("SIMCORE_PLAYER_TOKEN_SECRET", "p" * 16)
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError, match="SIMCORE_PLAYER_TOKEN_SECRET"):
            Settings(_env_file=None)
    finally:
        get_settings.cache_clear()


def test_development_still_allows_the_local_admin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIMCORE_ENV", "development")
    monkeypatch.delenv("SIMCORE_ADMIN_TOKEN", raising=False)
    get_settings.cache_clear()
    try:
        settings = Settings(_env_file=None)
        assert settings.admin_token == "dev-admin"
        assert settings.admin_enabled is True
    finally:
        get_settings.cache_clear()


def test_development_adds_localhost_origins_when_they_were_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIMCORE_ENV", "development")
    monkeypatch.setenv("SIMCORE_CORS_ORIGINS", "https://nustanakritwithai.github.io")
    get_settings.cache_clear()
    try:
        settings = Settings(_env_file=None)
        assert "https://nustanakritwithai.github.io" in settings.cors_origin_list
        assert "http://127.0.0.1:8080" in settings.cors_origin_list
    finally:
        get_settings.cache_clear()
