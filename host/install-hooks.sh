#!/bin/bash
# install-hooks.sh — Install Dark Army's notification hooks into Claude Code.
#
# Delegates to the canonical Python installer (dark_army_menubar.hooks) so the
# installed hook set never drifts from what the menu bar app installs. This:
#   1. writes the hook handler to ~/.dark-army/dark-army-notify, and
#   2. merges the full hook set into ~/.claude/settings.json without clobbering
#      any hooks you added yourself (idempotent, self-healing).
#
# Usage: cd host && ./install-hooks.sh
#   PYTHON=/path/to/python ./install-hooks.sh   # override the interpreter

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Prefer the host venv, then $PYTHON, then system python3.
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    if [ -x ".venv/bin/python" ]; then
        PY=".venv/bin/python"
    else
        PY="python3"
    fi
fi

"$PY" - <<'PYEOF'
from dark_army_menubar.hooks import install_notify_script, install_hooks

install_notify_script()
install_hooks()
from dark_army_daemon.paths import NOTIFY_SCRIPT_PATH
from pathlib import Path

print("Dark Army hooks installed into ~/.claude/settings.json")
print("Hook handler: ~/" + str(NOTIFY_SCRIPT_PATH.relative_to(Path.home())))
PYEOF

echo ""
echo "Restart any running Claude Code sessions for the hooks to take effect."
