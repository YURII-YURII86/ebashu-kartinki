#!/usr/bin/env bash
# Install the gpt-image2 skill for Claude Code.
#   bash install.sh            → ~/.claude/skills/gpt-image2
#   bash install.sh --project  → ./.claude/skills/gpt-image2 (current project only)
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
if [[ "${1:-}" == "--project" ]]; then
  DEST="$PWD/.claude/skills/gpt-image2"
else
  DEST="$HOME/.claude/skills/gpt-image2"
fi

echo "→ Installing to $DEST"
mkdir -p "$DEST"
rsync -a --delete --exclude '.git' --exclude '.venv' --exclude 'outputs' --exclude 'assets' \
  --exclude 'README*.md' "$SRC/" "$DEST/"

echo "→ Python environment for slides (.venv)"
if python3 -m venv "$DEST/.venv" 2>/dev/null; then
  "$DEST/.venv/bin/pip" install -q --upgrade pip
  "$DEST/.venv/bin/pip" install -q -r "$DEST/requirements.txt" && echo "  ✅ slide dependencies installed"
else
  echo "  ⚠️  could not create venv; single images still work, slides need: pip install -r requirements.txt"
fi

echo
python3 "$DEST/scripts/doctor.py" || true
echo
echo "Done. Restart Claude Code and ask: «нарисуй …» / «draw …»"
