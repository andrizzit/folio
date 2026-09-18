#!/bin/sh

set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BIN_DIR=${FOLIO_BIN_DIR:-"$HOME/.local/bin"}
CODEX_SKILLS_DIR=${FOLIO_CODEX_SKILLS_DIR:-"$HOME/.codex/skills"}
LAUNCHER_PATH=$BIN_DIR/folio
SKILL_PATH=$CODEX_SKILLS_DIR/folio

mkdir -p "$BIN_DIR" "$SKILL_PATH/agents" "$SKILL_PATH/assets"

if [ -e "$LAUNCHER_PATH" ] && [ ! -L "$LAUNCHER_PATH" ]; then
  echo "Refusing to replace non-symlink launcher: $LAUNCHER_PATH" >&2
  echo "Move it aside, then run install.sh again." >&2
  exit 1
fi

ln -sfn "$PROJECT_ROOT/integration/bin/folio" "$LAUNCHER_PATH"
install -m 0644 "$PROJECT_ROOT/integration/folio/SKILL.md" "$SKILL_PATH/SKILL.md"
install -m 0644 \
  "$PROJECT_ROOT/integration/folio/agents/openai.yaml" \
  "$SKILL_PATH/agents/openai.yaml"
install -m 0644 \
  "$PROJECT_ROOT/integration/folio/assets/folio-small.svg" \
  "$SKILL_PATH/assets/folio-small.svg"
install -m 0644 \
  "$PROJECT_ROOT/integration/folio/assets/folio.svg" \
  "$SKILL_PATH/assets/folio.svg"

echo "Installed Folio launcher at $LAUNCHER_PATH"
echo "Installed Codex skill at $SKILL_PATH"
