#!/usr/bin/env bash
# Полная проверка проекта одной командой — локально перед сдачей и в CI:
#
#   bash scripts/check.sh
#
# Шаги: ruff check, ruff check под Python 3.10 (как на ВМ), ruff format --check, pytest -q, scripts/smoke.py.
# Все шаги выполняются, даже если какой-то упал; в конце — итог и docs/test_report.md (дата, коммит,
# число тестов по файлам и областям, время). Код выхода 0 — всё зелёное, 1 — есть ошибки.
# Модель не вызывается: LLM_MODE=mock, без сети и без расходов.
# Python: переменная PYTHON, иначе .venv/bin/python проекта, иначе python3 из PATH (CI).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export LLM_MODE=mock

if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
elif [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  PY=python3
fi

WORK="$ROOT/.tmp/check"
mkdir -p "$WORK"
STEPS="$WORK/steps.tsv"
JUNIT="$WORK/junit.xml"
: > "$STEPS"
rm -f "$JUNIT"
FAILED=0

now() { "$PY" -c 'import time; print(f"{time.time():.2f}")'; }

step() {
  local name="$1"
  shift
  echo
  echo "=== $name"
  local start end status
  start="$(now)"
  if "$@"; then status=ok; else status=fail; FAILED=1; fi
  end="$(now)"
  printf '%s\t%s\t%s\n' "$name" "$status" "$("$PY" -c "print(round($end - $start, 1))")" >> "$STEPS"
}

step "ruff check" "$PY" -m ruff check .
step "ruff check, Python 3.10" "$PY" -m ruff check --target-version py310 .
step "ruff format --check" "$PY" -m ruff format --check .
step "pytest -q" "$PY" -m pytest -q --junitxml="$JUNIT" -o junit_family=xunit2
step "smoke.py" "$PY" scripts/smoke.py

echo
echo "=== Итог"
while IFS=$'\t' read -r name status seconds; do
  if [ "$status" = ok ]; then mark="✓"; else mark="✗"; fi
  printf '  %s %-26s %6s с\n' "$mark" "$name" "$seconds"
done < "$STEPS"

if "$PY" scripts/test_report.py --junit "$JUNIT" --steps "$STEPS" --out docs/test_report.md; then
  grep -m1 '^- Итог:' docs/test_report.md | sed 's/^- /  /; s/\*\*//g'
  echo "  Отчёт: docs/test_report.md"
else
  echo "  pytest не дошёл до конца — результатов тестов нет (так и записано в docs/test_report.md)"
  FAILED=1
fi

if [ "$FAILED" -ne 0 ]; then
  echo "ПРОВЕРКА НЕ ПРОЙДЕНА"
  exit 1
fi
echo "ВСЁ ЗЕЛЁНОЕ"
