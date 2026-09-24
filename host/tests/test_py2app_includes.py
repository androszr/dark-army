"""The frozen app carries what the import walker cannot see.

`cryptography`'s Rust extension loads `_cffi_backend` at runtime, so
py2app's walker never records it; the bundle then dies on its first
`from cryptography...` with `No module named '_cffi_backend'`. A venv
with more packages than requirements-dev.txt lists can hide the gap, so
the name is pinned in `setup.py` rather than trusted to the venv.
"""

from __future__ import annotations

import ast
from pathlib import Path

SETUP = Path(__file__).resolve().parents[1] / "setup.py"


def _options() -> dict:
    tree = ast.parse(SETUP.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            if "includes" in keys and "packages" in keys:
                return {
                    k.value: ast.literal_eval(v)
                    for k, v in zip(node.keys, node.values)
                    if isinstance(k, ast.Constant) and k.value in ("includes", "packages")
                }
    raise AssertionError("py2app OPTIONS dict not found in setup.py")


def test_cffi_backend_is_frozen_by_name():
    opts = _options()
    assert "_cffi_backend" in opts["includes"]
    assert "cryptography" in opts["packages"]
