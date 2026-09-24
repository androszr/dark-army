"""The photo-portrait tree's contract, and the split it introduces.

Two art trees, one roster. `assets/cast` is the menu-bar strip's hand-drawn
pixel art; `assets/portraits` is the still photographs the panel and the phone
draw. Both are keyed by the same twenty slugs (`identity.NAMES` lowercased
plus `identity.ART_ONLY`), and neither may add or rename one on its own.

Everything here is a seam that already exists: `tools/portrait_ingest.py`'s
`check()` over real or temporary trees, and greps over the two Swift copies —
the same shape `test_phone_theme_drift.py` relies on, because CI builds
neither client.
"""
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from dark_army_daemon.identity import ART_ONLY, NAMES

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "portrait_ingest.py"
PANEL_CAST = REPO / "panel" / "Sources" / "BobPanel" / "Cast.swift"
PHONE_CAST = REPO / "ios" / "BobPhone" / "Cast.swift"
PANEL_THEME = REPO / "panel" / "Sources" / "BobPanel" / "Theme.swift"
PHONE_THEME = REPO / "ios" / "BobPhone" / "Theme.swift"
PANEL_SPECIALISTS = REPO / "panel" / "Sources" / "BobPanel" / "Specialists.swift"
TREES = (
    REPO / "assets" / "portraits",
    REPO / "panel" / "Sources" / "BobPanel" / "Resources" / "portraits",
    REPO / "ios" / "BobPhone" / "Resources" / "portraits",
)
ROSTER = [n.lower() for n in NAMES] + list(ART_ONLY)


@pytest.fixture(scope="module")
def ingest():
    spec = importlib.util.spec_from_file_location("portrait_ingest", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ── the tool ─────────────────────────────────────────────────────────────────

def test_check_passes_on_the_tree_as_it_stands():
    """The shipped tree — empty, partial or full — is in step. `--check` is
    stdlib-only so this runs on a CI box with no Pillow."""
    out = subprocess.run([sys.executable, str(TOOL), "--check"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "in step" in out.stdout


def test_the_tool_reads_the_roster_out_of_identity(ingest):
    assert ingest.roster() == ROSTER
    assert ingest.MAX_PORTRAIT_BYTES == 600_000
    assert ingest.PORTRAIT_PX == 512


def test_the_three_copies_agree(ingest):
    assert ingest.check() == []


def _png(px: int, seed: int = 0) -> bytes:
    """A minimal valid PNG of the given side, distinct per seed."""
    import struct
    import zlib

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    row = b"\x00" + bytes([seed & 0xFF, 0, 0]) * px
    raw = row * px
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", px, px, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _trees(tmp_path, slugs=("cipher",), px=512):
    trees = tuple(tmp_path / name for name in ("a", "b", "c"))
    for tree in trees:
        tree.mkdir()
        for slug in slugs:
            (tree / f"{slug}.png").write_bytes(_png(px))
    return trees


def _write_manifests(ingest, trees):
    for tree in trees:
        ingest.write_manifest(tree, ROSTER)


def test_check_is_quiet_on_three_identical_trees(tmp_path, ingest):
    trees = _trees(tmp_path, ("cipher", "overwatch"))
    _write_manifests(ingest, trees)
    assert ingest.check(trees, ROSTER) == []


def test_check_names_a_slug_present_in_one_copy_and_not_another(tmp_path, ingest):
    trees = _trees(tmp_path, ("cipher",))
    (trees[2] / "cipher.png").unlink()
    _write_manifests(ingest, trees)
    problems = ingest.check(trees, ROSTER)
    assert any("cipher" in p and "missing from" in p for p in problems), problems


def test_check_names_a_byte_drift(tmp_path, ingest):
    trees = _trees(tmp_path, ("cipher",))
    (trees[1] / "cipher.png").write_bytes(_png(512, seed=7))
    _write_manifests(ingest, trees)
    problems = ingest.check(trees, ROSTER)
    assert any("not byte-identical" in p for p in problems), problems


def test_check_refuses_a_slug_outside_the_roster(tmp_path, ingest):
    trees = _trees(tmp_path, ("stranger",))
    _write_manifests(ingest, trees)
    problems = ingest.check(trees, ROSTER)
    assert any("stranger" in p and "not in the roster" in p for p in problems), problems


def test_check_refuses_a_portrait_of_the_wrong_size(tmp_path, ingest):
    trees = _trees(tmp_path, ("cipher",), px=64)
    _write_manifests(ingest, trees)
    problems = ingest.check(trees, ROSTER)
    assert any("not 512x512" in p for p in problems), problems


def test_check_refuses_a_manifest_that_does_not_describe_its_files(tmp_path, ingest):
    trees = _trees(tmp_path, ("cipher",))
    _write_manifests(ingest, trees)
    stale = json.loads(_read(trees[0] / "manifest.json"))
    stale["cast"] = []
    (trees[0] / "manifest.json").write_text(json.dumps(stale))
    problems = ingest.check(trees, ROSTER)
    assert any("does not describe" in p for p in problems), problems


def test_the_manifest_carries_no_animation_keys(ingest, tmp_path):
    """The photo tree has no `frame_ms`, `holds` or `states`: nothing ticks."""
    (trees := _trees(tmp_path, ("cipher",)))
    manifest = ingest.write_manifest(trees[0], ROSTER)
    assert set(manifest) == {"schema", "cast", "portraits"}
    assert manifest["cast"] == ["cipher"]
    entry = manifest["portraits"]["cipher"]
    assert set(entry) == {"width", "height", "bytes", "sha256"}
    assert (entry["width"], entry["height"]) == (512, 512)
    for tree in TREES:
        if (tree / "manifest.json").is_file():
            shipped = json.loads(_read(tree / "manifest.json"))
            assert set(shipped) == {"schema", "cast", "portraits"}
            assert "frame_ms" not in _read(tree / "manifest.json")


def test_mirror_makes_a_byte_copy_and_drops_strays(tmp_path, ingest):
    src = tmp_path / "src"
    src.mkdir()
    (src / "cipher.png").write_bytes(_png(512))
    ingest.write_manifest(src, ROSTER)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "stale.png").write_bytes(_png(512, seed=3))
    ingest.mirror(src, dest)
    assert sorted(p.name for p in dest.iterdir()) == ["cipher.png", "manifest.json"]
    assert (dest / "cipher.png").read_bytes() == (src / "cipher.png").read_bytes()


# ── the two Swift copies ─────────────────────────────────────────────────────

def _quoted(block: str) -> list[str]:
    return re.findall(r'"([^"\W\d_]+)"', block)


def test_neither_cast_copy_animates_or_reads_the_manifest():
    for path in (PANEL_CAST, PHONE_CAST):
        text = _read(path)
        for banned in ("TimelineView", "animates", "frame_ms", "manifest.json"):
            assert banned not in text, f"{path.name} still mentions {banned}"
        assert "portrait" in text


def test_both_copies_declare_the_same_art_only_list_as_python():
    lists = []
    for path in (PANEL_CAST, PHONE_CAST):
        text = _read(path)
        block = text.split("static let artOnly")[1].split("]")[0]
        lists.append(_quoted(block))
    assert lists[0] == lists[1] == list(ART_ONLY)


def test_both_copies_carry_the_roster():
    for path in (PANEL_CAST, PHONE_CAST):
        block = _read(path).split("static let names")[1].split("]")[0]
        assert _quoted(block) == list(NAMES), path


def test_both_copies_look_up_a_portrait_by_slug_and_cache_the_miss():
    panel = _read(PANEL_CAST)
    phone = _read(PHONE_CAST)
    # The panel resolves off the installed app (`PanelResources`, pinned by
    # `test_panel_resources.py`), never a bare `Bundle.module`.
    assert 'PanelResources.url(folder: "portraits"' in panel
    assert 'Bundle.main.url(forResource: "portraits"' in phone
    # The trap the phone file documents: `Bundle.module` may be *named* in a
    # comment there, never called.
    code = [l for l in phone.splitlines() if not l.lstrip().startswith("///")]
    assert not any("Bundle.module" in l for l in code), "the phone calls Bundle.module"
    for text in (panel, phone):
        assert "static func portrait(_ slug: String)" in text
        assert "portraits[key] = image" in text   # a miss is cached as nil too


def test_both_pixel_marks_draw_the_initial_when_no_portrait_exists():
    """A character with no photograph shows its initial on a plain tile —
    never a hashed stranger. Asserted by the shape of the branch."""
    for path in (PANEL_THEME, PHONE_THEME):
        text = _read(path)
        region = text.split("struct PixelMark")[1].split("\nstruct ")[0]
        assert "if let image = Cast.portrait(character)" in region, path
        assert "} else {" in region, path
        assert "initial(of: character)" in region, path
        assert "interpolation(.none)" not in region, path
        assert "interpolation(.high)" in region, path
        assert "StateRule(state: state" in region, path
        assert "Cast.character(" not in region, path   # no hashed substitute


def test_the_panel_avatar_view_is_a_still_with_the_state_rule():
    text = _read(PANEL_CAST)
    region = text.split("struct AvatarView")[1]
    assert "if let image = Cast.portrait(character)" in region
    assert "initial(of: character)" in region
    assert "StateRule(state: state" in region
    assert "interpolation(.high)" in region
    assert "clipShape(Circle())" in region


def test_the_state_rule_carries_form_before_colour():
    """Solid for work, dashed for sleep, red for alert — on both surfaces."""
    for path in (PANEL_CAST, PHONE_CAST):
        region = _read(path).split("struct StateRule")[1].split("\n}\n")[0]
        assert "case .work: return Theme.phosphor" in region, path
        assert "case .sleep: return Theme.faint" in region, path
        assert "case .alert: return Theme.alarm" in region, path
        assert "state == .sleep ? [3, 2] : []" in region, path


def test_area_lead_faces_follow_the_delivery_roster():
    """Area portraits are roster members; stages are markers until recorded."""
    from dark_army_daemon import areas
    for path in (REPO / "panel/Sources/BobPanel/Areas.swift", REPO / "ios/BobPhone/Areas.swift"):
        text = _read(path)
        rows = re.findall(r'Area\(slug: "([a-z]+)".*?pool: \[([^\]]+)\]', text)
        assert len(rows) == 8
        assert {slug: re.findall(r'"([^"]+)"', pool)[0] for slug, pool in rows} == {
            area.slug: area.pool[0] for area in areas.AREAS}
        assert all(area.pool[0] in ROSTER for area in areas.AREAS)
        assert "PixelMark(character: Areas.anchor(area.slug)" in text
    text = _read(PANEL_SPECIALISTS)
    assert len(re.findall(r'"(bc-[a-z-]+)": Known', text)) == 7
    assert 'if table[key] != nil { return "" }' in text
    assert "StageMark(name: stage.name" in text
    assert "PixelMark(character: leadFace" in text


# ── the six newcomers ───────────────────────────────────────────────────────

NEWCOMERS = ("androll", "captcha", "sawa", "franio", "zosia", "ptyś")


@pytest.mark.parametrize("slug", NEWCOMERS)
def test_a_newcomer_has_a_portrait_in_all_three_trees(slug, ingest):
    """The household characters were added with photographs in hand,
    so unlike the roster at large they are not allowed to be un-drawn: the
    initial-on-a-tile fallback is for a name whose picture has not been taken
    yet, and these six have been."""
    assert slug in [n.lower() for n in NAMES]
    for tree in TREES:
        path = tree / f"{slug}.png"
        assert path.is_file(), path
        data = path.read_bytes()
        assert ingest._png_size(data) == (512, 512), path
        assert len(data) <= ingest.MAX_PORTRAIT_BYTES, (path, len(data))


@pytest.mark.parametrize("slug", NEWCOMERS)
def test_a_newcomer_is_byte_identical_across_the_trees(slug):
    """The panel reads its copy through `PanelResources` and the phone reads
    its own through `Bundle.main`; two faces for one agent is the failure this
    catches, and `--check` cannot say which slug drifted."""
    first, *rest = [(tree / f"{slug}.png").read_bytes() for tree in TREES]
    for other in rest:
        assert other == first, slug


@pytest.mark.parametrize("slug", NEWCOMERS)
def test_a_newcomer_is_described_by_every_manifest(slug):
    for tree in TREES:
        manifest = json.loads(_read(tree / "manifest.json"))
        assert slug in manifest["cast"], tree
        entry = manifest["portraits"][slug]
        assert (entry["width"], entry["height"]) == (512, 512), tree
        assert entry["bytes"] == (tree / f"{slug}.png").stat().st_size, tree


def test_the_roster_is_twenty_slugs():
    """Twenty assignable names plus the planner's art-only face."""
    assert len(ROSTER) == 21
    assert ROSTER[-1] == "overwatch"
    assert set(NEWCOMERS) < set(ROSTER)
    # The newcomers are assignable, never art-only.
    assert not set(NEWCOMERS) & set(ART_ONLY)


# ── bundling ─────────────────────────────────────────────────────────────────

def test_the_portrait_tree_reaches_both_bundles_and_not_py2app():
    package = _read(REPO / "panel" / "Package.swift")
    assert '.copy("Resources/portraits")' in package
    build = _read(REPO / "host" / "build.sh")
    assert "assets/portraits" in build
    # Above the signing step: anything written
    # into Resources after signing breaks the seal.
    assert build.index("assets/portraits") < build.index("codesign")
    pbx = _read(REPO / "ios" / "BobPhone.xcodeproj" / "project.pbxproj")
    assert "Resources/portraits" in pbx
    # Its `resources` list is `dark_army_menubar/icons` alone; a second
    # copy through py2app would drift from the panel's.
    assert "assets/portraits" not in _read(REPO / "host" / "setup.py")


def test_every_shipped_tree_holds_a_manifest():
    """An empty tree still ships its manifest, which is what keeps the two
    resource folders present for SwiftPM and Xcode."""
    for tree in TREES:
        assert (tree / "manifest.json").is_file(), tree
