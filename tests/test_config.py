"""Настройки: LLM_MODE=auto выбирает live при заданном шлюзе, иначе mock с предупреждением; явные режимы — как есть."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings, app_version, git_commit

URL = "https://gateway.example/api/v1"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Чистое окружение без ключей шлюза; файл настроек проекта не читается (env_file=None)."""
    for name in ("LLM_MODE", "OPENAI_BASE_URL", "OPENAI_API_KEY"):
        monkeypatch.setenv(name, "")  # запоминает исходное значение: после теста окружение восстановится,
        monkeypatch.delenv(name)  # даже если load_dotenv добавил переменную сам
    return monkeypatch


def test_auto_is_default_and_falls_back_to_mock_with_warning(env: pytest.MonkeyPatch) -> None:
    settings = Settings.from_env(env_file=None)
    assert settings.llm_mode == "mock" and settings.llm_mode_requested == "auto"
    assert not settings.llm_ready and not settings.gateway_configured
    assert settings.llm_warning and "OPENAI_API_KEY" in settings.llm_warning


def test_auto_with_gateway_is_live(env: pytest.MonkeyPatch) -> None:
    env.setenv("OPENAI_BASE_URL", URL + "/")
    env.setenv("OPENAI_API_KEY", "секрет-для-теста")
    settings = Settings.from_env(env_file=None)
    assert settings.llm_mode == "live" and settings.llm_ready and settings.llm_warning is None
    assert settings.base_url == URL
    assert "секрет-для-теста" not in repr(settings)


def test_auto_with_only_url_stays_mock(env: pytest.MonkeyPatch) -> None:
    env.setenv("OPENAI_BASE_URL", URL)
    assert Settings.from_env(env_file=None).llm_mode == "mock"


@pytest.mark.parametrize("mode", ["mock", "replay", "live"])
def test_explicit_modes_are_kept(env: pytest.MonkeyPatch, mode: str) -> None:
    env.setenv("OPENAI_BASE_URL", URL)
    env.setenv("OPENAI_API_KEY", "k")
    env.setenv("LLM_MODE", mode.upper())
    settings = Settings.from_env(env_file=None)
    assert settings.llm_mode == mode and settings.llm_ready == (mode == "live")
    assert (settings.llm_warning is None) == (mode == "live")


def test_explicit_live_without_gateway_warns(env: pytest.MonkeyPatch) -> None:
    env.setenv("LLM_MODE", "live")
    settings = Settings.from_env(env_file=None)
    assert settings.llm_mode == "live" and not settings.llm_ready
    assert settings.llm_warning and "ошибкой" in settings.llm_warning


def test_unknown_mode_is_rejected(env: pytest.MonkeyPatch) -> None:
    env.setenv("LLM_MODE", "prod")
    with pytest.raises(ValueError, match="auto, mock, live или replay"):
        Settings.from_env(env_file=None)


def test_env_file_does_not_override_environment(env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings_file = tmp_path / "local.env"
    settings_file.write_text("LLM_MODE=live\nOPENAI_BASE_URL=https://from-file.example\n", encoding="utf-8")
    env.setenv("LLM_MODE", "mock")
    settings = Settings.from_env(env_file=settings_file)
    assert settings.llm_mode == "mock" and settings.base_url == "https://from-file.example"


def test_git_commit_reads_head_and_packed_refs(tmp_path: Path) -> None:
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "packed-refs").write_text("# pack-refs\n0633257abcdef refs/heads/main\n", encoding="utf-8")
    assert git_commit(tmp_path) == "0633257"
    (git / "refs" / "heads" / "main").write_text("abcdef1234567\n", encoding="utf-8")
    assert git_commit(tmp_path) == "abcdef1"
    assert git_commit(tmp_path / "nowhere") == ""
    assert app_version().startswith("0.")
