"""Last-known rate-limit windows, so a chip does not vanish.

Grok's billing fetch and Codex's journals can both go quiet while the account
window is still open. The panel then draws nothing, which reads as a broken
feature. This file is the last reading we will stand behind, per provider —
written when a real figure arrives, read when the live source has none.

Not a second computation of a percentage: whatever Grok or Codex last
reported is stored verbatim. `stale` is recomputed on read from `resets_at`.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

from .paths import STATE_DIR, USAGE_HOLD_PATH, ensure_state_dir

logger = logging.getLogger("dark-army.usage-hold")

_lock = threading.Lock()


def load(provider: str, *, path: Optional[Path] = None) -> dict:
    """The last stored payload for `provider`, or ``{}``."""
    target = path if path is not None else USAGE_HOLD_PATH
    with _lock:
        data = _read(target)
    held = data.get(provider)
    return dict(held) if isinstance(held, dict) else {}


def save(provider: str, payload: dict, *, path: Optional[Path] = None) -> None:
    """Remember `payload` as the last good reading for `provider`."""
    if not isinstance(payload, dict) or not payload:
        return
    target = path if path is not None else USAGE_HOLD_PATH
    with _lock:
        data = _read(target)
        data[provider] = dict(payload)
        _write(target, data)


def _read(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write(path: Path, data: dict) -> None:
    parent = path.parent
    if parent == STATE_DIR:
        ensure_state_dir()
    else:
        parent.mkdir(parents=True, exist_ok=True)
    tmp_path = ""
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(parent), suffix=".tmp")
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
        os.replace(tmp_path, str(path))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        logger.warning("could not persist usage hold to %s", path)
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
