#!/usr/bin/env bash
# Установка навыка ЕБАШУ КАРТИНКИ для Claude Code.
#   bash install.sh            → ~/.claude/skills/ebashu-kartinki
#   bash install.sh --project  → ./.claude/skills/ebashu-kartinki (только для текущего проекта)
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
if [[ "${1:-}" == "--project" ]]; then
  DEST="$PWD/.claude/skills/ebashu-kartinki"
else
  DEST="$HOME/.claude/skills/ebashu-kartinki"
fi

echo "→ Ставлю в $DEST"
mkdir -p "$DEST"
rsync -a --delete --exclude '.git' --exclude '.venv' --exclude 'outputs' --exclude 'assets' \
  --exclude 'README*.md' "$SRC/" "$DEST/"

echo "→ Окружение Python для презентаций (.venv)"
if python3 -m venv "$DEST/.venv" 2>/dev/null; then
  "$DEST/.venv/bin/pip" install -q --upgrade pip
  "$DEST/.venv/bin/pip" install -q -r "$DEST/requirements.txt" && echo "  ✅ зависимости для презентаций установлены"
else
  echo "  ⚠️  не удалось создать окружение; картинки работают, для презентаций нужно: pip install -r requirements.txt"
fi

echo
python3 "$DEST/scripts/doctor.py" || true
echo
echo "Готово. Перезапустите Claude Code и пишите: «нарисуй …»"
