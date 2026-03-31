#!/usr/bin/env bash
# Forgememo integration setup
# Wires forgememo hook.py into your AI coding tool of choice.
# Usage: bash integrations/setup.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

echo "Forgememo integration setup"
echo "==========================="
echo ""
echo "Which tool do you want to integrate?"
echo "  1) Claude Code  (~/.claude/settings.json)"
echo "  2) Codex CLI    (~/.codex/config.yaml)"
echo "  3) OpenCode     (~/.config/opencode/config.json)"
echo "  4) Gemini CLI   (~/.gemini/settings.json)"
echo ""
read -r -p "Choice [1-4]: " CHOICE

# Resolve the python executable that has forgememo installed
PYTHON="${PYTHON:-$(command -v python3 || command -v python)}"
if ! "$PYTHON" -c "import forgememo" 2>/dev/null; then
  echo "ERROR: forgememo is not importable by $PYTHON"
  echo "  Run: pip install -e $REPO_ROOT"
  exit 1
fi

case "$CHOICE" in
1)
  TARGET="$HOME/.claude/settings.json"
  SNIPPET="$SCRIPT_DIR/claude-code/settings-snippet.json"
  echo ""
  echo "Merging hooks into $TARGET ..."
  if [ ! -f "$TARGET" ]; then
    cp "$SNIPPET" "$TARGET"
    echo "Created $TARGET with forgememo hooks."
  else
    echo ""
    echo "settings.json already exists. Merge the following block into the"
    echo "\"hooks\" key of $TARGET:"
    echo ""
    cat "$SNIPPET"
    echo ""
    echo "(Auto-merge skipped to avoid overwriting existing hooks.)"
  fi
  ;;
2)
  TARGET="$HOME/.codex/config.yaml"
  SNIPPET="$SCRIPT_DIR/codex/config-snippet.yaml"
  echo ""
  echo "Review the snippet and merge manually into $TARGET:"
  echo ""
  cat "$SNIPPET"
  echo ""
  echo "NOTE: Verify the hook key names against your installed Codex version."
  ;;
3)
  TARGET="$HOME/.config/opencode/config.json"
  SNIPPET="$SCRIPT_DIR/opencode/config-snippet.json"
  echo ""
  echo "Review the snippet and merge manually into $TARGET:"
  echo ""
  cat "$SNIPPET"
  echo ""
  echo "NOTE: Verify hook key names against your installed OpenCode version."
  ;;
4)
  TARGET="$HOME/.gemini/settings.json"
  SNIPPET="$SCRIPT_DIR/gemini/settings-snippet.json"
  echo ""
  echo "Review the snippet and merge manually into $TARGET:"
  echo ""
  cat "$SNIPPET"
  echo ""
  echo "NOTE: Verify hook key names against your installed Gemini CLI version."
  ;;
*)
  echo "Invalid choice."
  exit 1
  ;;
esac

echo ""
echo "After configuring hooks, start the daemon:"
echo "  python -m forgememo.daemon &"
echo ""
echo "Verify it's running:"
echo "  curl --unix-socket /tmp/forgememo.sock http://localhost/health"
