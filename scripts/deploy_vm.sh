#!/usr/bin/env bash
# Выкладка с ноутбука на ВМ. Запускается вручную в терминале (ssh спросит два пароля: бастион, затем команда).
# Режим --rsync дополнительно исключает всё из локального .git/info/exclude (файлы, которых не должно быть в репо).
#
#   bash scripts/deploy_vm.sh            # Gitea доступен с ноутбука: push → на ВМ git pull → установка → перезапуск
#   bash scripts/deploy_vm.sh --rsync    # Gitea с ноутбука недоступен: копируем файлы по ssh → на ВМ commit+push → перезапуск
#   bash scripts/deploy_vm.sh --check    # только узнать Python и состояние ВМ
set -euo pipefail

JUMP="jump@84.54.59.198"
VM="tochka-sborki-2026-team-093@10.250.0.96"
MODE="${1:-push}"
cd "$(dirname "$0")/.."
LOCAL_EXCLUDE=""
[ -f .git/info/exclude ] && LOCAL_EXCLUDE=".git/info/exclude"

case "$MODE" in
  --check)
    ssh -J "$JUMP" "$VM" 'cd ~/project 2>/dev/null && pwd; python3 --version; free -h | head -2; df -h ~ | tail -1; git -C ~/project log --oneline -3 2>/dev/null || true'
    ;;
  --rsync)
    command -v rsync >/dev/null || { echo "Нужен rsync (или используйте Upload в Jupyter)"; exit 1; }
    rsync -az --delete \
      --exclude '.git/' --exclude '.venv/' --exclude '.cache/' --exclude '.tmp/' --exclude 'cache/' \
      --exclude 'logs/' --exclude 'eval/runs/' --exclude 'data/sessions/' --exclude '.env' \
      ${LOCAL_EXCLUDE:+--exclude-from="$LOCAL_EXCLUDE"} \
      -e "ssh -J $JUMP" ./ "$VM:~/project/"
    ssh -J "$JUMP" "$VM" 'bash -s -- --commit' < scripts/vm_update.sh
    ;;
  push|*)
    git push origin main
    ssh -J "$JUMP" "$VM" 'bash -s -- --pull' < scripts/vm_update.sh
    ;;
esac
