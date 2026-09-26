"""The design token source, generated copies, and native routes stay in sync."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
GENERATOR = TOOLS / "design_system_tokens.py"
SOURCE = ROOT / "design-system/tokens.json"
WORKSHOP = ROOT / "design-system/workshop"


def load_generator():
    spec = importlib.util.spec_from_file_location("design_system_tokens", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_token_source_validates():
    source = json.loads(SOURCE.read_text())
    generator = load_generator()

    assert generator.validate(source) == source


@pytest.mark.parametrize(
    "edit",
    (
        lambda source: source.update(unexpected=True),
        lambda source: source["colors"].update(unexpected="#123456"),
        lambda source: source["colors"].update(canvas="not-a-color"),
        lambda source: source["size"].update(phoneTarget=30),
    ),
    ids=("unknown-key", "unknown-color", "invalid-color", "phone"),
)
def test_malformed_source_is_refused(edit):
    source = json.loads(SOURCE.read_text())
    edit(source)
    with pytest.raises(ValueError):
        load_generator().validate(source)


def test_generated_outputs_match_generator_check():
    result = subprocess.run(
        ["python3", str(GENERATOR), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_native_token_copies_match_generated_outputs():
    generator = load_generator()
    outputs = generator.generated(json.loads(SOURCE.read_text()))

    for path, expected in outputs.items():
        if "panel" in str(path).lower() or "ios" in str(path).lower():
            assert path.is_file()
            assert path.read_text() == expected


def test_workshop_html_blocks_network_and_inline_scripts():
    pages = tuple(WORKSHOP.rglob("*.html"))
    assert pages

    for page in pages:
        html = page.read_text()
        assert re.search(r"Content-Security-Policy[^>]*", html, re.IGNORECASE)
        policy = re.search(r'<meta[^>]*http-equiv="Content-Security-Policy"[^>]*content="([^"]+)"',
                           html, re.IGNORECASE)
        assert policy is not None
        directives = policy.group(1).lower()
        assert "connect-src 'none'" in directives
        assert "https:" not in directives
        assert "http:" not in directives
        assert "'unsafe-inline'" not in directives
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html, re.IGNORECASE)


def test_workshop_copies_contain_required_files():
    copies = (ROOT / "panel/Sources/BobPanel/Resources/workshop",
              ROOT / "ios/BobPhone/Resources/workshop")

    required = ("index.html", "tokens.json", "tokens.css", "tokens.js",
                "workshop.css", "workshop.js")
    for copy in copies:
        for name in required:
            assert (copy / name).is_file(), f"{copy} is missing {name}"


@pytest.mark.parametrize(
    "relative,needles",
    (
        (
            "panel/Sources/BobPanel/SettingsMenuModel.swift",
            ("design system", "designsystem"),
        ),
        (
            "ios/BobPhone/MenuView.swift",
            ("design system", "signalworkshopphone"),
        ),
    ),
)
def test_native_entry_routes_exist(relative: str, needles: tuple[str, ...]):
    source = (ROOT / relative).read_text().lower()

    assert all(needle in source for needle in needles)


def test_webview_file_navigation_is_restricted_to_bundled_root():
    for path in (ROOT / "panel/Sources/BobPanel/SettingsView.swift",
                 ROOT / "ios/BobPhone/MenuView.swift"):
        guarded = path.read_text()
        assert "WKNavigationDelegate" in guarded
        assert "allowingReadAccessTo" in guarded
        assert "standardizedFileURL" in guarded
        assert "path.hasPrefix(root.path + \"/\")" in guarded
        assert "url.isFileURL" in guarded


def test_workshop_layout_fits_a_phone_width():
    """The page's grid column must be allowed to shrink below its widest
    content: the terminal specimen is one `nowrap` line, and an `auto`
    track sized to it pushed every panel past a 393pt phone screen (the
    Reset button and the second token column cut off)."""
    css = (WORKSHOP / "workshop.css").read_text(encoding="utf-8")
    assert "main { display: grid; grid-template-columns: minmax(0, 1fr);" in css
    assert ".panel { min-width: 0;" in css
    assert ".specimen { min-width: 0;" in css
    assert "minmax(min(100%, 150px), 1fr)" in css
