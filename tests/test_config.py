"""Настройки: LLM_MODE=auto выбирает live при заданном шлюзе, иначе mock с предупреждением; явные режимы — как есть."""

from __future__ import annotations

import subprocess
import sys
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
    env.delenv("LLM_DAILY_BUDGET_RUB", raising=False)
    settings = Settings.from_env(env_file=None)
    assert Settings().daily_budget_rub == settings.daily_budget_rub == 280
    assert settings.llm_mode == "mock" and settings.llm_mode_requested == "auto"
    assert not settings.llm_ready and not settings.gateway_configured
    assert settings.llm_warning and "OPENAI_API_KEY" in settings.llm_warning


@pytest.mark.parametrize(("budget", "expected"), [(None, 280), ("175", 175)])
@pytest.mark.parametrize("short", [False, True])
def test_budget_script_uses_configured_daily_limit(
    env: pytest.MonkeyPatch, tmp_path: Path, budget: str | None, expected: int, short: bool
) -> None:
    env.setenv("LLM_MODE", "mock")
    env.setenv("PYTHON_DOTENV_DISABLED", "1")
    env.setenv("LLM_DAILY_LIMIT_RUB", "999")
    env.delenv("LLM_DAILY_BUDGET_RUB", raising=False)
    if budget is not None:
        env.setenv("LLM_DAILY_BUDGET_RUB", budget)
    script = Path(__file__).resolve().parents[1] / "scripts" / "budget.py"
    command = [sys.executable, str(script), "--log", str(tmp_path / "cost.jsonl")]
    if short:
        command.append("--short")
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    assert f"из {expected} ₽" in result.stdout


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


def test_version_in_docker_image_comes_from_build_arg(tmp_path: Path) -> None:
    """В образе нет .git: коммит берётся из APP_COMMIT (--build-arg GIT_COMMIT); .git, если есть, важнее."""
    from app.config import APP_VERSION

    no_git = tmp_path / "image"
    assert app_version(no_git, {"APP_COMMIT": "9ebdd5e"}) == f"{APP_VERSION}+9ebdd5e"
    assert app_version(no_git, {"APP_COMMIT": " 9ebdd5e\n; rm"}) == f"{APP_VERSION}+9ebdd5erm"
    assert app_version(no_git, {"APP_COMMIT": ""}) == APP_VERSION
    assert app_version(no_git, {}) == APP_VERSION
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("abcdef1234567\n", encoding="utf-8")
    assert app_version(tmp_path, {"APP_COMMIT": "9ebdd5e"}) == f"{APP_VERSION}+abcdef1"


def test_generation_limits_come_from_environment(env: pytest.MonkeyPatch) -> None:
    """Пороги генерации — из окружения; без переменных — проверенные значения по умолчанию."""
    from app.config import DEFAULT_LIMITS
    from app.pipeline import content
    from app.pipeline.default import DefaultPipeline
    from tests.validate_kits import make_pk_kit

    assert Settings.from_env(env_file=None).limits == DEFAULT_LIMITS
    env.setenv("QUESTIONS_PER_LO", "3")
    env.setenv("BUDGET_MARGIN", "2")
    env.setenv("LITERATURE_MAX_MAIN", "3")
    settings = Settings.from_env(env_file=None)
    assert settings.limits.questions_per_lo == 3 and settings.limits.literature_per_section()["main"] == 3
    generator = DefaultPipeline(settings.model_copy(update={"llm_mode": "mock"})).content
    assert generator.limits.budget_margin == 2
    kit = make_pk_kit()
    quotas = content.question_quotas(kit, generator.limits)
    assert max(n for q in quotas.values() for n in q.values()) == 3
    assert content.choose_mode(10, 5, 15, generator.limits.budget_margin)[0].value == "economy"  # 15 < 10 × 2


def test_invalid_limit_is_reported_by_name(env: pytest.MonkeyPatch) -> None:
    env.setenv("FA_PASS_SHARE", "1.5")
    with pytest.raises(ValueError, match="FA_PASS_SHARE"):
        Settings.from_env(env_file=None)
