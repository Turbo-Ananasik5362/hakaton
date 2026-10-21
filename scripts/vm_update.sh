#!/usr/bin/env bash
# Выполняется НА ВМ (его передаёт scripts/deploy_vm.sh через ssh). Тоже всё внутри ~/project:
# своё .venv, кеш pip в ~/project/.cache, временные файлы в ~/project/.tmp.
#   --pull     git pull из Gitea (код запушен с ноутбука) — по умолчанию
#   --commit   код пришёл через rsync: закоммитить и запушить в main с ВМ
#   --restart  без git: только установка зависимостей и перезапуск (откат, docs/deploy.md)
set -euo pipefail
cd ~/project
mkdir -p .cache/pip .tmp logs
export PIP_CACHE_DIR="$HOME/project/.cache/pip" TMPDIR="$HOME/project/.tmp" PIP_REQUIRE_VIRTUALENV=1 PIP_DISABLE_PIP_VERSION_CHECK=1

# После отката (git checkout --detach <коммит>) HEAD не на ветке: --pull возвращается на main, --commit отказывается
on_branch() { git symbolic-ref -q HEAD >/dev/null; }

case "${1:---pull}" in
  --pull)    on_branch || git checkout -q main; git pull --ff-only origin main ;;
  --commit)  on_branch || { echo "ВМ на прошлом коммите (откат): сначала git checkout main" >&2; exit 2; }
             git add -A; git commit -m "deploy from laptop $(date +%F_%H%M)" || true; git push origin main ;;
  --restart) ;;
  *)         echo "Неизвестный режим: $1 (нужен --pull, --commit или --restart)" >&2; exit 2 ;;
esac

[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/python -m pip install -q -r requirements.txt

# Перезапуск сервера на 0.0.0.0:8000 (демо и автопрогоны)
pkill -f "uvicorn app.main:app" 2>/dev/null || true
sleep 1
LLM_MODE=live nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 > logs/server.log 2>&1 &
sleep 4
if health=$(curl -fsS http://127.0.0.1:8000/health); then
  echo "$health"
  echo "OK: сервер поднят, коммит $(git rev-parse --short HEAD)"
  case "$health" in
    *'"llm_ready":true'*) ;;
    *) echo "ВНИМАНИЕ: llm_ready=false — сервер не ходит в GigaChat (нет OPENAI_BASE_URL / OPENAI_API_KEY в окружении?)." ;;
  esac
else
  echo "Сервер не ответил на /health — последние строки лога:"; tail -n 40 logs/server.log; exit 1
fi
