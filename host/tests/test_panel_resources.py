"""The panel finds its art in the installed app, never in the checkout.

SwiftPM's `Bundle.module` accessor hard-codes the building checkout's
`.build` path into the binary and otherwise looks beside
`Bundle.main.bundleURL`, which for the nested `BobPanel.app` is not where
`build.sh` puts the bundle. An installed panel was reading portraits out of
a deleted worktree and drawing initials. `PanelResources` resolves the
bundle off the running app first; every lookup goes through it.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PANEL = REPO / "panel" / "Sources" / "BobPanel"
BUILD_SH = REPO / "host" / "build.sh"


def test_only_the_resolver_names_bundle_module():
    offenders = []
    for path in sorted(PANEL.glob("*.swift")):
        if path.name == "PanelResources.swift":
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"\bBundle\.module\b", line):
                offenders.append(f"{path.name}:{lineno}")
    assert offenders == [], offenders


def test_resolver_tries_the_installed_app_before_swiftpm():
    src = (PANEL / "PanelResources.swift").read_text()
    resources = src.index("if let resources = main.resourceURL")
    bundle_url = src.index("out.append(main.bundleURL")
    fallback = src.index("return Bundle.module")
    assert resources < bundle_url < fallback


def test_build_puts_the_bundle_where_the_resolver_looks():
    src = BUILD_SH.read_text()
    assert 'cp -R "$PANEL_BUNDLE" "$PANEL_APP/Contents/Resources/"' in src
