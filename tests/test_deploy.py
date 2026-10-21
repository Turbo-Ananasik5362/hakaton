"""Развёртывание без сборки образа: Dockerfile, .dockerignore, docker-compose.yml и docs/deploy.md.

Docker на машине разработчика может не быть, поэтому тест читает файлы и сверяет их с кодом и README:
все пути COPY существуют и не отброшены .dockerignore, всё, что сервер читает, попадает в образ, а всё, куда он пишет,
доступно непривилегированному пользователю и вынесено в тома; команда запуска и порт — как в README и на ВМ;
HEALTHCHECK смотрит на настоящий /health. Сама сборка проверяется вручную там, где есть Docker (docs/deploy.md).
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
from pathlib import Path
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from app.config import ROOT, Settings
from app.main import create_app

DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
COMPOSE = ROOT / "docker-compose.yml"
DEPLOY_DOC = ROOT / "docs" / "deploy.md"
README = ROOT / "README.md"
VM_UPDATE = ROOT / "scripts" / "vm_update.sh"
START = ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]


# --- разбор Dockerfile ---------------------------------------------------------------------------------------------


def instructions(text: str) -> list[tuple[str, str]]:
    """Инструкции Dockerfile (ИМЯ, аргументы) с учётом переноса строк «\\» и комментариев."""
    result: list[tuple[str, str]] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            buffer += line[:-1].strip() + " "
            continue
        word, _, rest = (buffer + line).partition(" ")
        result.append((word.upper(), rest.strip()))
        buffer = ""
    assert not buffer, "Dockerfile кончается переносом строки"
    return result


def args_of(rest: str) -> list[str]:
    """Аргументы инструкции: JSON-массив (exec-форма) или строка оболочки."""
    return json.loads(rest) if rest.startswith("[") else shlex.split(rest)


@pytest.fixture(scope="module")
def docker() -> list[tuple[str, str]]:
    return instructions(DOCKERFILE.read_text(encoding="utf-8"))


def one(docker: list[tuple[str, str]], name: str) -> str:
    found = [rest for word, rest in docker if word == name]
    assert len(found) == 1, f"{name}: ожидалась одна инструкция, найдено {len(found)}"
    return found[0]


def workdir(docker: list[tuple[str, str]]) -> str:
    return one(docker, "WORKDIR")


def copies(docker: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """(путь в репозитории, путь в образе) для каждого источника COPY из контекста сборки."""
    result = []
    for word, rest in docker:
        if word not in ("COPY", "ADD"):
            continue
        assert word == "COPY", "ADD не нужен: используйте COPY"
        assert not any(a.startswith("--from") for a in args_of(rest)), "многоэтапной сборки нет"
        *sources, dest = [a for a in args_of(rest) if not a.startswith("--")]
        target = posixpath.normpath(posixpath.join(workdir(docker), dest))
        into_dir = dest.endswith("/") or dest in (".", "./") or len(sources) > 1
        for source in sources:
            source = posixpath.normpath(source)
            # папка копируется своим содержимым в dest; файл — в dest/имя, если dest — папка
            if (ROOT / source).is_file() and into_dir:
                result.append((source, posixpath.join(target, posixpath.basename(source))))
            else:
                result.append((source, target))
    return result


def image_path(docker: list[tuple[str, str]], rel: str) -> str | None:
    """Где в образе окажется файл репозитория rel; None — его нет в образе."""
    for source, target in copies(docker):
        if rel == source or rel.startswith(source + "/"):
            return posixpath.join(target, rel[len(source) :].lstrip("/")).rstrip("/")
    return None


def runtime_dirs(settings: Settings) -> dict[str, str]:
    """Куда сервер пишет: путь относительно корня проекта."""
    return {
        "сессии": settings.sessions_dir.relative_to(ROOT).as_posix(),
        "кеш модели": settings.cache_dir.relative_to(ROOT).as_posix(),
        "журнал расходов": settings.cost_log.parent.relative_to(ROOT).as_posix(),
    }


# --- .dockerignore: правила Docker (filepath.Match + «**»), последнее совпадение решает -------------------------------


def _rule_regex(pattern: str) -> re.Pattern[str]:
    pattern = pattern.strip("/")
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out)


def ignore_rules() -> list[tuple[bool, re.Pattern[str]]]:
    rules = []
    for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        rules.append((negate, _rule_regex(line.lstrip("!"))))
    return rules


def ignored(rel: str, rules: list[tuple[bool, re.Pattern[str]]]) -> bool:
    """Файл или папка rel не попадает в контекст сборки: правило совпало с ним или с одной из его папок."""
    parts = rel.split("/")
    prefixes = ["/".join(parts[: n + 1]) for n in range(len(parts))]
    result = False
    for negate, regex in rules:
        if any(regex.fullmatch(p) for p in prefixes):
            result = not negate
    return result


# --- тесты ---------------------------------------------------------------------------------------------------------


def test_base_image_is_python_310_slim_like_vm(docker: list[tuple[str, str]]) -> None:
    assert one(docker, "FROM") == "python:3.10-slim"
    assert workdir(docker) == "/app"


def test_dependencies_installed_without_pip_cache(docker: list[tuple[str, str]]) -> None:
    runs = [rest for word, rest in docker if word == "RUN"]
    assert any("pip install --no-cache-dir -r requirements.txt" in run for run in runs)
    order = [(w, r) for w, r in docker if w in ("COPY", "RUN")]
    first_copy = next(r for w, r in order if w == "COPY")
    assert args_of(first_copy)[0] == "requirements.txt", "зависимости — отдельным слоем до кода"


def test_copy_sources_exist_and_reach_build_context(docker: list[tuple[str, str]]) -> None:
    rules = ignore_rules()
    sources = [source for source, _ in copies(docker)]
    assert sources, "в Dockerfile нет COPY"
    for source in sources:
        assert (ROOT / source).exists(), f"COPY {source}: такого пути в репозитории нет"
        assert not ignored(source, rules), f"COPY {source}: путь отброшен .dockerignore — сборка упадёт"


def test_files_inside_copied_dirs_are_not_dropped_by_dockerignore(docker: list[tuple[str, str]]) -> None:
    """Каждый файл кода и данных из скопированных папок (кроме данных работающего сервера) попадает в образ."""
    rules = ignore_rules()
    skip_dirs = {"__pycache__", "sessions"}
    dropped = []
    for source, _ in copies(docker):
        path = ROOT / source
        if path.is_file():
            continue
        for folder, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
            for name in files:
                if name.startswith(".") or name.endswith(".pyc"):
                    continue
                rel = (Path(folder) / name).relative_to(ROOT).as_posix()
                if ignored(rel, rules):
                    dropped.append(rel)
    assert not dropped, f"не попадут в образ: {dropped[:10]}"


def test_everything_the_server_reads_is_in_the_image(docker: list[tuple[str, str]]) -> None:
    """Корень проекта в образе — WORKDIR: app/config.py ищет данные относительно папки над app/."""
    from app.render.docx_pk import TEMPLATE
    from app.skills import SKILLS_DIR
    from app.web.report_page import TEMPLATES

    settings = Settings(llm_mode="mock")
    needed = {
        "код": ROOT / "app" / "main.py",
        "навыки": SKILLS_DIR,
        "шаблоны страниц": TEMPLATES,
        "реестр": settings.registry_dir,
        "шапка организации": settings.organization_file,
        "шаблон .docx": TEMPLATE,
        "заготовки mock": settings.fixtures_dir,
    }
    for what, path in needed.items():
        rel = path.relative_to(ROOT).as_posix()
        assert path.exists(), f"{what}: {rel} нет в репозитории"
        assert image_path(docker, rel) == posixpath.join(workdir(docker), rel), (
            f"{what}: {rel} не попадает в образ"
        )


def test_server_runs_unprivileged_and_can_write_its_data(docker: list[tuple[str, str]]) -> None:
    user = one(docker, "USER")
    assert user not in ("root", "0") and not user.startswith(("0:", "root:"))
    names = [word for word, _ in docker]
    runs = [(i, rest) for i, (word, rest) in enumerate(docker) if word == "RUN"]
    created = next(i for i, rest in runs if re.search(rf"useradd\b.*\b{re.escape(user)}\b", rest))
    setup_at, setup = next((i, rest) for i, rest in runs if "chown" in rest)
    last_copy = max(i for i, word in enumerate(names) if word == "COPY")
    assert created < names.index("USER") < names.index("CMD"), f"пользователь {user} создаётся до USER"
    assert last_copy < setup_at < names.index("USER"), "права выставляются после COPY и до USER"

    made = re.search(r"mkdir -p ([^&]+)", setup)
    owned = re.search(rf"chown -R {re.escape(user)}:\S+ ([^&]+)", setup)
    assert made and owned, "каталоги данных сервера создаются и отдаются пользователю"
    made_dirs, owned_dirs = made.group(1).split(), owned.group(1).split()
    for what, rel in runtime_dirs(Settings(llm_mode="mock")).items():
        assert rel in made_dirs, f"{what}: mkdir -p {rel}"
        assert any(rel == d or rel.startswith(d + "/") for d in owned_dirs), (
            f"{what}: {rel} не принадлежит {user}"
        )


def test_copied_files_are_readable_by_server_user(docker: list[tuple[str, str]]) -> None:
    """COPY сохраняет права из папки сборщика: у кого-то шаблон .docx — 0600, и karkas его не прочитает."""
    setup = next(rest for word, rest in docker if word == "RUN" and "chown" in rest)
    readable = re.search(r"chmod -R a\+rX ([^&]+)", setup)
    assert readable, "chmod -R a+rX для скопированного кода и данных"
    targets = readable.group(1).split()
    root = workdir(docker)
    for source, image in copies(docker):
        if source == "requirements.txt":  # нужен только pip при сборке (от root)
            continue
        rel = posixpath.relpath(image, root)
        assert any(rel == t or rel.startswith(t + "/") for t in targets), f"{source}: нет chmod a+rX"


def test_start_command_and_port_match_readme_and_vm(docker: list[tuple[str, str]]) -> None:
    cmd = args_of(one(docker, "CMD"))
    assert cmd == START
    port = cmd[cmd.index("--port") + 1]
    assert one(docker, "EXPOSE") == port

    command = " ".join(START)
    readme = README.read_text(encoding="utf-8")
    assert command in readme.split("## Развёртывание", 1)[1], (
        "README, «Развёртывание»: команда запуска как в образе"
    )
    for line in re.findall(r"uvicorn app\.main:app[^\n`]*", readme):
        assert f"--port {port}" in line, f"README: {line}"
    assert f"127.0.0.1:{port}/health" in readme
    assert command in DEPLOY_DOC.read_text(encoding="utf-8")
    assert command in VM_UPDATE.read_text(encoding="utf-8"), "на ВМ сервер запускается той же командой"


def test_healthcheck_requests_real_health_endpoint(docker: list[tuple[str, str]], tmp_path: Path) -> None:
    check = one(docker, "HEALTHCHECK")
    options, _, command = check.partition("CMD")
    assert "--interval=" in options and "--timeout=" in options
    args = args_of(command.strip())
    assert args[:2] == ["python", "-c"], "в python:3.10-slim нет curl — проверка на Python"
    compile(args[2], "HEALTHCHECK", "exec")
    assert "ProxyHandler({})" in args[2], "HTTP(S)_PROXY из окружения не должен перехватывать запрос к себе"
    url = urlparse(re.search(r"https?://[^'\"\s]+", args[2]).group(0))
    cmd = args_of(one(docker, "CMD"))
    assert url.hostname == "127.0.0.1" and str(url.port) == cmd[cmd.index("--port") + 1]
    assert url.path == "/health"

    app = create_app(Settings(llm_mode="mock", sessions_dir=tmp_path / "sessions"))
    with TestClient(app) as client:
        response = client.get(url.path)
    assert response.status_code == 200 and response.json()["status"] == "ok"
    doc = DEPLOY_DOC.read_text(encoding="utf-8")
    for field in response.json():
        assert f"`{field}`" in doc, f"docs/deploy.md: поле /health {field} не описано"


def test_no_secrets_baked_into_image(docker: list[tuple[str, str]]) -> None:
    assert not [rest for _, rest in docker if "OPENAI_" in rest], (
        "ключи шлюза — только из окружения при запуске"
    )
    rules = ignore_rules()
    for secret in (".env", ".env.local"):
        assert ignored(secret, rules)
    assert "OPENAI_" not in COMPOSE.read_text(encoding="utf-8")


def test_dockerignore_drops_local_and_runtime_data() -> None:
    rules = ignore_rules()
    assert not [r.pattern for negate, r in rules if not negate and r.fullmatch(".")], (
        "правило задевает корень «.»"
    )
    for rel in (
        ".venv/bin/python",
        ".cache/pip/x",
        ".tmp/check/junit.xml",
        "cache/llm/abc.json",
        "logs/llm_cost.jsonl",
        "data/sessions/s_0123456789ab/state.json",
        "eval/runs/20261021-1640/q3/kit.json",
        ".env",
        ".git/HEAD",
        ".idea/workspace.xml",  # любые скрытые папки локальных инструментов и редакторов
        ".vscode/settings.json",
        "app/__pycache__/main.cpython-310.pyc",
    ):
        assert ignored(rel, rules), f".dockerignore пропускает {rel}"
    for rel in (
        "app/main.py",
        "app/skills/patch.md",
        "data/registry",
        "tests/fixtures/llm",
        "requirements.txt",
    ):
        assert not ignored(rel, rules), f".dockerignore отбрасывает нужное: {rel}"


def test_compose_publishes_port_and_keeps_server_data_in_volumes(docker: list[tuple[str, str]]) -> None:
    yaml = pytest.importorskip("yaml")  # PyYAML приходит с uvicorn[standard]
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    (name, service), *rest = compose["services"].items()
    assert not rest and name == "karkas"
    assert compose["name"] == "karkas", "имена томов не зависят от имени папки проекта"
    cmd = args_of(one(docker, "CMD"))
    port = cmd[cmd.index("--port") + 1]
    assert f"{port}:{port}" in service["ports"]
    assert service["build"]["context"] == "."
    assert "GIT_COMMIT" in service["build"]["args"], "коммит для version в /health"
    assert {"path": ".env", "required": False} in service["env_file"], "необязательный .env"

    targets = {}
    for volume in service["volumes"]:
        source, target = volume.split(":")[:2]
        assert source in compose["volumes"], (
            f"{source}: именованный том (права из образа, без bind-монтирования)"
        )
        targets[target] = source
    for what, rel in runtime_dirs(Settings(llm_mode="mock")).items():
        path = posixpath.join(workdir(docker), rel)
        assert any(path == t or path.startswith(t + "/") for t in targets), f"{what}: {path} не в томе"


def test_vm_update_has_restart_mode_for_rollback() -> None:
    text = VM_UPDATE.read_text(encoding="utf-8")
    modes = text.split('case "${1:---pull}" in', 1)[1].split("esac", 1)[0]
    assert re.search(r"^\s*--restart\)", modes, re.M), (
        "vm_update.sh --restart: перезапуск без git pull (откат)"
    )
    unknown = re.search(r"^\s*\*\)(.*)$", modes, re.M)
    assert unknown and "Неизвестный режим" in unknown.group(1) and "exit 2" in unknown.group(1)
    pull = re.search(r"^\s*--pull\)(.*)$", modes, re.M).group(1)
    assert "git checkout -q main" in pull, "после отката (detached HEAD) --pull возвращается на main"


def test_deploy_doc_covers_three_ways_check_logs_and_rollback() -> None:
    doc = DEPLOY_DOC.read_text(encoding="utf-8")
    for heading in (
        "## Способ 1 — ВМ хакатона",
        "## Способ 2 — Docker",
        "## Способ 3 — локально",
        "## Проверка после выкладки",
        "## Где логи и сессии",
        "## Откат на прошлый коммит",
    ):
        assert heading in doc, heading
    for needle in ("scripts/vm_update.sh", "docker compose up -d --build", "logs/server.log", "git revert"):
        assert needle in doc, needle
    assert "push --force" not in doc and "push -f" not in doc and "reset --hard" not in doc
    section = README.read_text(encoding="utf-8").split("## Развёртывание", 1)[1].split("\n## ", 1)[0]
    assert "docs/deploy.md" in section
