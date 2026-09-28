# host/tests/test_image_preview.py
"""The phone's picture read: `image_preview.locate`'s confinement (project
root, `..`, symlinks, hidden folders, extensions, the home folder), the
tail search, `render`'s re-encoding (never a file's own bytes except a real
animated GIF, always under the frame budget, an SVG that reaches for other
files refused), and the sealed `image` kind on both doors (a read: no action
tuple, no lease, no record)."""

from __future__ import annotations

import base64
import inspect
import json
import os
from pathlib import Path

import pytest
from PIL import Image

from dark_army_daemon import conversation, image_preview as ip, relay
from dark_army_daemon.api_server import ApiServer
from dark_army_daemon.daemon import BobDaemon


@pytest.fixture(autouse=True)
def onboarded(tmp_path, monkeypatch):
    """The test project (`home/proj`) stands in for an onboarded project;
    a test narrows, widens or empties the list itself."""
    roots = [str(tmp_path / "home" / "proj")]
    monkeypatch.setattr(ip, "_onboarded", lambda: list(roots))
    return roots


@pytest.fixture
def home(tmp_path):
    folder = tmp_path / "home"
    folder.mkdir()
    return folder


@pytest.fixture
def project(home):
    root = home / "proj"
    (root / ".git").mkdir(parents=True)
    (root / "host").mkdir()
    (root / "assets" / "girl").mkdir(parents=True)
    Image.new("RGB", (40, 20), (200, 10, 10)).save(
        root / "assets" / "girl" / "01.jpg")
    Image.new("RGBA", (30, 30), (0, 255, 0, 100)).save(
        root / "assets" / "clear.png")
    return root


def _preview(project, path, cwd=None, home=None):
    return ip.preview(str(cwd or project), path,
                      home=home or project.parent)


def test_the_project_root_is_the_git_folder_above_the_working_folder(
        project, home):
    assert ip.project_root(str(project / "host"), home=home) == project.resolve()


def test_the_home_folder_and_root_are_never_a_project(home, tmp_path):
    assert ip.project_root(str(home), home=home) is None
    assert ip.project_root("/", home=home) is None
    assert ip.project_root("", home=home) is None
    assert ip.project_root("relative/folder", home=home) is None
    loose = home / "loose"
    loose.mkdir()
    # No `.git` anywhere below the home folder: the working folder itself,
    # and the walk never climbs into the home folder.
    (home / ".git").mkdir()
    assert ip.project_root(str(loose), home=home) == loose.resolve()


def test_a_relative_path_is_found_from_the_working_folder_and_the_root(
        project):
    got = _preview(project, "assets/girl/01.jpg", cwd=project / "host")
    assert got["available"] is True, got
    assert got["path"] == "assets/girl/01.jpg"
    assert got["name"] == "01.jpg"
    assert (got["width"], got["height"]) == (40, 20)


def test_a_tail_of_a_path_is_found_inside_the_project(project):
    got = _preview(project, "girl/01.jpg")
    assert got["available"] is True, got
    assert got["path"] == "assets/girl/01.jpg"


@pytest.mark.parametrize("path, reason", [
    ("../outside.png", ip.OUTSIDE),
    ("/etc/hosts.png", ip.OUTSIDE),
    ("~/secret.png", ip.OUTSIDE),
    ("assets/../../outside.png", ip.OUTSIDE),
    ("README.md", ip.NOT_A_PICTURE),
    ("assets/girl/01.jpg.txt", ip.NOT_A_PICTURE),
    ("nope/missing.png", ip.MISSING),
    ("", ip.MISSING),
    ("a\x00b.png", ip.MISSING),
])
def test_refusals_are_in_words(project, home, path, reason):
    Image.new("RGB", (4, 4)).save(home / "outside.png")
    Image.new("RGB", (4, 4)).save(home / "secret.png")
    got = _preview(project, path, home=home)
    assert got["available"] is False
    assert got["reason"] == reason
    assert "data" not in got


def test_a_symlink_leaving_the_project_is_refused(project, home):
    Image.new("RGB", (4, 4)).save(home / "elsewhere.png")
    os.symlink(home / "elsewhere.png", project / "assets" / "link.png")
    got = _preview(project, "assets/link.png", home=home)
    assert got["available"] is False and got["reason"] == ip.OUTSIDE


def test_a_symlinked_folder_leaving_the_project_is_refused(project, home):
    away = home / "away"
    away.mkdir()
    Image.new("RGB", (4, 4)).save(away / "x.png")
    os.symlink(away, project / "assets" / "door")
    got = _preview(project, "assets/door/x.png", home=home)
    assert got["available"] is False and got["reason"] == ip.OUTSIDE
    # And the tail search never walks through it.
    got = _preview(project, "door/x.png", home=home)
    assert got["available"] is False


def test_hidden_folders_are_refused_and_never_searched(project):
    hidden = project / ".secret"
    hidden.mkdir()
    Image.new("RGB", (4, 4)).save(hidden / "k.png")
    assert _preview(project, ".secret/k.png")["reason"] == ip.HIDDEN
    assert _preview(project, "k.png")["available"] is False


def test_a_session_outside_any_project_shows_nothing(home):
    Image.new("RGB", (4, 4)).save(home / "a.png")
    got = ip.preview(str(home), "a.png", home=home)
    assert got["available"] is False and got["reason"] == ip.NO_PROJECT


def test_a_file_named_like_a_picture_is_decoded_not_sent(project):
    (project / "assets" / "fake.png").write_text("TOKEN=hunter2")
    got = _preview(project, "assets/fake.png")
    assert got["available"] is False and got["reason"] == ip.UNREADABLE
    assert "hunter2" not in json.dumps(got)


def test_a_picture_is_re_encoded_and_transparency_kept(project):
    got = _preview(project, "assets/clear.png")
    assert got["available"] is True and got["format"] == "png"
    data = base64.b64decode(got["data"])
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data != (project / "assets" / "clear.png").read_bytes()


def test_a_large_picture_is_shrunk_under_the_budget(project):
    noisy = Image.effect_noise((3000, 2200), 90).convert("RGB")
    noisy.save(project / "assets" / "big.jpg", quality=95)
    got = _preview(project, "assets/big.jpg")
    assert got["available"] is True, got
    assert len(base64.b64decode(got["data"])) <= ip.PREVIEW_MAX_BYTES
    assert max(got["shown_width"], got["shown_height"]) <= ip.MAX_PIXELS
    assert (got["width"], got["height"]) == (3000, 2200)
    assert got["reduced"] is True


def test_a_small_animated_gif_travels_as_itself(project):
    frames = [Image.new("RGB", (20, 10), (i * 50, 0, 0)) for i in range(4)]
    path = project / "assets" / "spin.gif"
    frames[0].save(path, save_all=True, append_images=frames[1:],
                   duration=80, loop=0)
    got = _preview(project, "assets/spin.gif")
    assert got["available"] is True
    assert got["format"] == "gif" and got["animated"] is True
    assert got["frames"] == 4
    assert base64.b64decode(got["data"]) == path.read_bytes()


def test_a_still_gif_is_re_encoded(project):
    Image.new("RGB", (20, 10)).save(project / "assets" / "still.gif")
    got = _preview(project, "assets/still.gif")
    assert got["available"] is True and got["animated"] is False
    assert got["format"] != "gif"


SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">'
       '<rect width="200" height="100" fill="red"/>{extra}</svg>')


def test_an_svg_is_drawn_on_the_mac(project):
    (project / "assets" / "logo.svg").write_text(SVG.format(extra=""))
    got = _preview(project, "assets/logo.svg")
    assert got["available"] is True, got
    assert got["format"] in ("png", "jpeg")
    assert (got["width"], got["height"]) == (200, 100)
    assert got["shown_width"] == 2 * got["shown_height"]


@pytest.mark.parametrize("extra", [
    '<image href="file:///etc/x.png"/>',
    '<image xlink:href="https://example.com/a.png"/>',
    '<style>@import "x.css";</style>',
    '<rect style="fill:url(other.svg#p)"/>',
    '<foreignObject></foreignObject>',
])
def test_an_svg_reaching_for_other_files_is_refused(project, extra):
    (project / "assets" / "evil.svg").write_text(SVG.format(extra=extra))
    got = _preview(project, "assets/evil.svg")
    assert got["available"] is False and got["reason"] == ip.SVG_REACHES


def test_an_svg_with_inline_data_and_fragments_is_drawn(project):
    extra = ('<rect style="fill:url(#g)"/>'
             '<use href="#g"/><image href="data:image/png;base64,AAAA"/>')
    (project / "assets" / "ok.svg").write_text(SVG.format(extra=extra))
    assert ip._svg_refusal(SVG.format(extra=extra).encode()) == ""


# --- the sealed kind ---------------------------------------------------------

@pytest.fixture
def server():
    daemon = BobDaemon()
    srv = ApiServer(daemon, port=0)
    daemon._api = srv
    return srv, daemon


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["home", "away"])
async def test_the_image_kind_is_a_read_on_both_doors(server, project, door,
                                                      monkeypatch):
    srv, daemon = server
    actions = srv.LAN_ACTIONS if door == "home" else srv.REMOTE_ACTIONS
    monkeypatch.setattr(ip.Path, "home", staticmethod(lambda: project.parent))
    daemon._agents_snapshot_cache = {"running": [
        {"session_id": "s1", "cwd": str(project / "host")}]}
    monkeypatch.setattr(relay, "lease_valid", lambda _d: False)

    async def run(payload):
        return await srv._sealed_run(
            "image", payload, "phone-1", actions=actions,
            check_lease=door == "away", record=door == "away")

    status, _, body = await run({"session": "s1", "path": "girl/01.jpg"})
    assert status == 200, body
    got = json.loads(body)
    assert got["available"] is True and got["path"] == "assets/girl/01.jpg"
    status, _, body = await run({"session": "nobody", "path": "a.png"})
    assert status == 200
    assert json.loads(body)["reason"] == conversation.UNKNOWN_SESSION_REFUSAL
    for payload in ({}, {"session": "s1"}, {"path": "a.png"},
                    {"session": "s1", "path": "x" * 5000 + ".png"},
                    {"session": ["s1"], "path": "a.png"}):
        status, _, _ = await run(payload)
        assert status == 400
    assert list(getattr(daemon, "_remote_activity", [])) == []


def test_the_lan_door_admits_the_kind_above_the_action_branch():
    assert '"image"' in inspect.getsource(ApiServer._lan_home)
    src = inspect.getsource(ApiServer._sealed_run)
    refusal = src.index('self._bot_refusal(device_id, "read")')
    image = src.index('if kind == "image":')
    assert refusal < image < src.index('if kind == "action":')


def test_no_action_tuple_grew():
    assert "image" not in ApiServer.LAN_ACTIONS
    assert "image" not in ApiServer.REMOTE_ACTIONS


def test_the_budget_fits_one_sealed_frame():
    # base64 is 4/3 of the bytes; the reply's JSON and the frame's own
    # fields need room beside it under the plaintext cap.
    assert ip.PREVIEW_MAX_BYTES * 4 / 3 + 50_000 < relay.RELAY_FRAME_MAX_BYTES


def test_the_file_is_read_off_the_loop():
    src = inspect.getsource(BobDaemon.image_preview)
    assert "run_in_executor" in src and "image_preview.preview" in src


# --- the security review's findings ------------------------------------------

def test_a_folder_holding_the_home_folder_is_never_a_project(tmp_path,
                                                             onboarded):
    users = tmp_path / "Users"
    me = users / "me"
    (me / "Pictures").mkdir(parents=True)
    Image.new("RGB", (4, 4)).save(me / "Pictures" / "x.png")
    assert ip.project_root(str(users), home=me) is None
    got = ip.preview(str(users), "me/Pictures/x.png", home=me)
    assert got["available"] is False and got["reason"] == ip.NO_PROJECT
    # Nor a `.git` found above the home folder by a walk from outside it:
    # the walk stops below it and the working folder alone is the project.
    (users / ".git").mkdir()
    side = users / "shared"
    side.mkdir()
    assert ip.project_root(str(side), home=me) == side.resolve()
    onboarded[:] = [str(side)]
    assert ip.preview(str(side), "../me/Pictures/x.png",
                      home=me)["reason"] == ip.OUTSIDE


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32"])
def test_an_svg_not_in_utf8_is_refused(project, encoding):
    svg = SVG.format(extra='<image href="file:///etc/x.png"/>')
    (project / "assets" / "wide.svg").write_bytes(svg.encode(encoding))
    got = _preview(project, "assets/wide.svg")
    assert got["available"] is False and got["reason"] == ip.SVG_REACHES


def test_an_svg_declaring_another_encoding_is_refused():
    data = b'<?xml version="1.0" encoding="ISO-8859-1"?>' + SVG.format(
        extra="").encode()
    assert ip._svg_refusal(data) == ip.SVG_REACHES
    ok = b'<?xml version="1.0" encoding="UTF-8"?>' + SVG.format(
        extra="").encode()
    assert ip._svg_refusal(ok) == ""


def test_a_picture_claiming_too_many_pixels_is_not_decoded(project,
                                                          monkeypatch):
    monkeypatch.setattr(ip, "SOURCE_MAX_PIXELS", 40 * 20 - 1)
    got = _preview(project, "assets/girl/01.jpg")
    assert got["available"] is False and got["reason"] == ip.TOO_LARGE


def test_a_heavy_animation_travels_as_its_first_frame(project, monkeypatch):
    frames = [Image.new("RGB", (20, 10), (i * 50, 0, 0)) for i in range(4)]
    path = project / "assets" / "heavy.gif"
    frames[0].save(path, save_all=True, append_images=frames[1:],
                   duration=80, loop=0)
    monkeypatch.setattr(ip, "GIF_MAX_TOTAL_PIXELS", 20 * 10 * 4 - 1)
    got = _preview(project, "assets/heavy.gif")
    assert got["available"] is True and got["animated"] is True
    assert got["format"] != "gif"


def test_the_search_stops_at_its_bound(project):
    for i in range(30):
        (project / "assets" / f"n{i}.txt").write_text("")
    assert ip._search(project, ("girl", "01.jpg"), limit=5) == []
    assert ip._search(project, ("girl", "01.jpg"))


@pytest.mark.asyncio
async def test_pictures_are_read_one_at_a_time(monkeypatch):
    import asyncio
    import threading
    daemon = BobDaemon()
    daemon._agents_snapshot_cache = {"running": [
        {"session_id": "s1", "cwd": "/nowhere"}]}
    live = []
    peak = []
    lock = threading.Lock()

    def slow(cwd, path):
        with lock:
            live.append(1)
            peak.append(len(live))
        import time
        time.sleep(0.05)
        with lock:
            live.pop()
        return {"available": False}

    monkeypatch.setattr(ip, "preview", slow)
    await asyncio.gather(*(daemon.image_preview("s1", f"{i}.png")
                           for i in range(4)))
    assert max(peak) == 1


# --- the bug audit's findings ------------------------------------------------

def test_rendering_drains_its_own_autorelease_pool():
    src = inspect.getsource(ip.preview)
    assert "objc.autorelease_pool()" in src
    assert src.index("autorelease_pool") < src.index("render(file, root)")


def test_repeated_renders_on_one_thread_keep_memory_flat(project):
    import resource
    from concurrent.futures import ThreadPoolExecutor
    (project / "assets" / "logo.svg").write_text(SVG.format(extra=""))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_preview, project, "assets/logo.svg").result()
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        for _ in range(15):
            assert pool.submit(_preview, project,
                               "assets/logo.svg").result()["available"]
        after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is bytes on macOS; without the pool this grew ~35 MB each.
    assert after - before < 150 * 1024 * 1024


def test_an_absolute_path_through_a_symlinked_folder_is_judged_where_it_leads(
        project, home):
    alias = home / "alias"
    os.symlink(project, alias)
    got = _preview(project, str(alias / "assets" / "girl" / "01.jpg"),
                   home=home)
    assert got["available"] is True, got


def test_a_path_walking_up_inside_the_project_is_allowed(project):
    got = _preview(project, "../assets/girl/01.jpg", cwd=project / "host")
    assert got["available"] is True, got


def test_any_image_library_failure_is_a_refusal(project, monkeypatch):
    def boom(file, info):
        raise RuntimeError("objc.error")
    monkeypatch.setattr(ip, "_raster", boom)
    got = _preview(project, "assets/girl/01.jpg")
    assert got["available"] is False and got["reason"] == ip.UNREADABLE


# --- the security re-review ---------------------------------------------------

def test_the_home_holder_is_judged_by_identity_not_spelling(tmp_path):
    users = tmp_path / "Users"
    me = users / "me"
    me.mkdir(parents=True)
    # The Mac's disk ignores letter case: `/users` is `/Users`.
    other = Path(str(users).replace("/Users", "/users"))
    assert other.is_dir()
    assert ip.project_root(str(other), home=me) is None
    # And a second path to the same folder (a symlink standing in for the
    # `/System/Volumes/Data` firmlink) is the same folder.
    alias = tmp_path / "alias"
    os.symlink(tmp_path, alias)
    assert ip.project_root(str(alias / "Users"), home=me) is None


def test_a_project_under_home_still_finds_its_root(project, home):
    assert ip.project_root(str(project / "host"), home=home) == project.resolve()
    loose = home / "notes"
    loose.mkdir()
    assert ip.project_root(str(loose), home=home) == loose.resolve()


def test_an_unquoted_reference_is_refused():
    assert ip._svg_refusal(
        SVG.format(extra="<image href=file:///x.png/>").encode()) \
        == ip.SVG_REACHES


# --- the phone keeps nothing ---------------------------------------------------

PHONE = Path(__file__).resolve().parents[2] / "ios" / "BobPhone"


def _phone(name):
    return (PHONE / name).read_text()


def test_only_the_opened_picture_is_asked_of_the_mac():
    calls = [p.name for p in PHONE.glob("*.swift")
             if "client.imagePreview(" in p.read_text()]
    assert calls == ["ConversationView.swift"]
    view = _phone("ConversationView.swift")
    sheet = view[view.index("struct PhoneImageSheetView"):]
    assert sheet.count("client.imagePreview(") == 1
    assert "let path: String" in sheet and "paths" not in sheet


def test_the_phone_never_saves_a_picture():
    view = _phone("ConversationView.swift")
    for forbidden in ("ShareLink", "UIImageWriteToSavedPhotosAlbum",
                      "PHPhotoLibrary", ".write(to"):
        assert forbidden not in view, forbidden
    client = _phone("Client.swift")
    fetch = client[client.index("func imagePreview("):]
    fetch = fetch[:fetch.index("\n    }\n")]
    assert "write" not in fetch and "FileManager" not in fetch
    memo = _phone("Models.swift")
    memo = memo[memo.index("struct ImageMemo"):]
    assert "static let lifetime: TimeInterval = 24 * 60 * 60" in memo
    assert "FileManager" not in memo and "UserDefaults" not in memo
    assert "imageMemo.prune()" in client


def test_a_root_holding_home_under_another_name_serves_nothing_from_home(
        tmp_path, monkeypatch):
    # `/System/Volumes/Data` holds `/Users/me` as `Data/Users/me` through a
    # firmlink no test can make: stand in a root `project_root` failed to
    # recognise, and the file's own walk up to it must still meet home.
    data = tmp_path / "Data"
    me = data / "Users" / "me"
    (me / "Pictures").mkdir(parents=True)
    Image.new("RGB", (4, 4)).save(me / "Pictures" / "3.jpg")
    Image.new("RGB", (4, 4)).save(data / "own.png")
    monkeypatch.setattr(ip, "project_root",
                        lambda cwd, home=None: data.resolve())
    for path in ("Users/me/Pictures/3.jpg", str(me / "Pictures" / "3.jpg"),
                 "Pictures/3.jpg"):
        got = ip.preview(str(data), path, home=me)
        assert got["available"] is False, (path, got)
    # Fail closed: a root that holds home serves nothing at all.
    assert ip.preview(str(data), "own.png", home=me)["available"] is False


def test_real_system_volume_paths_serve_nothing_from_home():
    me = Path.home()
    for cwd in ("/System/Volumes/Data", "/System/Volumes",
                "/System/Volumes/Data/Users"):
        if not Path(cwd).is_dir():
            continue
        rel = str(me.relative_to("/")) if cwd == "/System/Volumes/Data" \
            else "Data/" + str(me.relative_to("/"))
        root = ip.project_root(cwd)
        if root is None:
            continue
        file, _root, _reason = ip.locate(cwd, rel + "/nothing-here.png")
        assert file is None
        # Onboarding the volume itself opens nothing either: a whole disk
        # is never a project, judged against the real home folder.
        assert ip.locate(cwd, rel + "/nothing-here.png",
                         projects=[cwd])[2] == ip.NOT_ONBOARDED


# ── only onboarded projects ──────────────────────────────────────────────


def test_a_folder_that_is_not_onboarded_shows_nothing(project, home,
                                                      onboarded):
    desktop = home / "Desktop"
    (desktop / "shots" / ".git").mkdir(parents=True)
    Image.new("RGB", (4, 4)).save(desktop / "shots" / "s.png")
    onboarded[:] = [str(project)]
    got = ip.preview(str(desktop / "shots"), "s.png", home=home)
    assert got["available"] is False and got["reason"] == ip.NOT_ONBOARDED
    # The onboarded project itself still shows its pictures.
    assert _preview(project, "assets/clear.png", home=home)["available"]


def test_a_working_folder_inside_an_onboarded_project_is_allowed(
        project, home, onboarded):
    onboarded[:] = [str(project)]
    got = ip.preview(str(project / "host"), "../assets/clear.png", home=home)
    assert got["available"] is True


def test_an_absolute_path_out_of_an_onboarded_project_is_refused(
        project, home, onboarded):
    docs = home / "Documents"
    docs.mkdir()
    Image.new("RGB", (4, 4)).save(docs / "d.png")
    onboarded[:] = [str(project)]
    got = _preview(project, str(docs / "d.png"), home=home)
    assert got["available"] is False and got["reason"] == ip.OUTSIDE


def test_onboarding_the_home_folder_or_above_never_opens_it(home, onboarded):
    (home / "Desktop").mkdir()
    Image.new("RGB", (4, 4)).save(home / "Desktop" / "a.png")
    onboarded[:] = [str(home), str(home.parent), "/"]
    got = ip.preview(str(home / "Desktop"), "a.png", home=home)
    assert got["available"] is False and got["reason"] == ip.NOT_ONBOARDED


def test_an_empty_or_unreadable_list_shows_nothing(project, home,
                                                   onboarded, monkeypatch):
    onboarded[:] = []
    assert _preview(project, "assets/clear.png",
                    home=home)["reason"] == ip.NOT_ONBOARDED
    from dark_army_daemon import enrollment
    monkeypatch.undo()

    def broken():
        raise OSError("ledger gone")
    monkeypatch.setattr(enrollment, "enrolled_roots", broken)
    assert ip._onboarded() == []


def test_the_list_is_the_enrolled_projects_and_dark_armys_checkout(
        monkeypatch):
    from dark_army_daemon import enrollment
    monkeypatch.undo()
    monkeypatch.setattr(enrollment, "enrolled_roots",
                        lambda: {"/w/a", "", "/w/b"})
    monkeypatch.setattr(enrollment, "self_root", lambda: "/w/self")
    assert ip._onboarded() == ["/w/a", "/w/b", "/w/self"]


def test_the_daemon_passes_no_list_so_the_ledger_decides():
    src = inspect.getsource(BobDaemon.image_preview)
    assert "image_preview.preview, cwd, path)" in src


def test_an_onboarded_whole_disk_is_ignored(project, home, onboarded,
                                            monkeypatch):
    onboarded[:] = [str(project)]
    real = os.path.ismount
    monkeypatch.setattr(ip.os.path, "ismount",
                        lambda p: Path(p) == project.resolve() or real(p))
    assert _preview(project, "assets/clear.png",
                    home=home)["reason"] == ip.NOT_ONBOARDED
    monkeypatch.setattr(ip.os.path, "ismount", real)
    assert _preview(project, "assets/clear.png", home=home)["available"]


def test_a_real_volume_root_is_never_onboarded():
    for disk in ("/", "/System/Volumes/Data"):
        if os.path.isdir(disk):
            held = ip._identities(Path.home().resolve())
            assert ip._in_projects(Path(disk).resolve(), [disk],
                                   held) is None


def test_an_onboarded_subfolder_of_a_bigger_repository_opens_only_itself(
        tmp_path, onboarded):
    home = tmp_path / "home"
    repo = home / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "app" / "src").mkdir(parents=True)
    (repo / "other").mkdir()
    Image.new("RGB", (4, 4)).save(repo / "app" / "a.png")
    Image.new("RGB", (4, 4)).save(repo / "other" / "b.png")
    onboarded[:] = [str(repo / "app")]
    cwd = str(repo / "app" / "src")
    assert ip.preview(cwd, "a.png", home=home)["available"] is True
    assert ip.preview(cwd, "../a.png", home=home)["available"] is True
    got = ip.preview(cwd, str(repo / "other" / "b.png"), home=home)
    assert got["available"] is False and got["reason"] == ip.OUTSIDE
    # A session working in the repository above the onboarded folder is
    # not inside a project.
    assert ip.preview(str(repo), "app/a.png",
                      home=home)["reason"] == ip.NOT_ONBOARDED


def test_a_corrupt_entry_is_skipped_not_raised(project, home, onboarded):
    onboarded[:] = ["/bad\x00entry", 7, "", "relative/x", str(project)]
    assert _preview(project, "assets/clear.png", home=home)["available"]
    onboarded[:] = ["/bad\x00entry"]
    assert _preview(project, "assets/clear.png",
                    home=home)["reason"] == ip.NOT_ONBOARDED


def test_a_folder_holding_home_under_another_name_is_a_whole_disk(tmp_path):
    data = tmp_path / "Data"
    me = data / "Users" / "me"
    me.mkdir(parents=True)
    alias = tmp_path / "Users"
    alias.symlink_to(data / "Users")
    home = alias / "me"
    held = ip._identities(home.resolve())
    assert ip._whole_disk(data.resolve(), held, home) is True
    assert ip._whole_disk((tmp_path / "Data").resolve().parent / "Data"
                          / "Users" / "me" / ".." / "..", held, home) is True
    other = tmp_path / "proj"
    other.mkdir()
    assert ip._whole_disk(other.resolve(), held, home) is False


def test_closing_a_picture_returns_to_the_same_conversation():
    """The picture is its own sheet over the conversation, never a rung on
    the phone's sheet trail: a rung replaces the agent screen, which came
    back rebuilt on Main at the top."""
    view = _phone("ConversationView.swift")
    comm = _phone("CommView.swift")
    host = _phone("PhoneSheetHost.swift")
    assert "sheets.show(.image" not in view + comm
    assert "case image" not in host and "PhoneImageSheetView" not in host
    screen = view[view.index("struct ConversationScreen"):
                  view.index("struct ConversationTurnRow")]
    assert "@State private var picture: PicturePick?" in screen
    assert ".picturePopup($picture, client: client)" in screen
    # On the screen's outer stack, beside the reply box, not inside the
    # scrolling page whose onAppear jumps to the bottom.
    body = screen[screen.index("var body: some View"):]
    body = body[:body.index("private var page")]
    assert ".picturePopup($picture, client: client)" in body
    chips = view[view.index("struct ConversationImageChips"):]
    chips = chips[:chips.index("\n}\n")]
    assert "resignFirstResponder" in chips
    pane = comm[comm.index("struct HelperConversationPane"):]
    assert ".picturePopup($picture, client: client)" in pane
    popup = view[view.index("struct PicturePopup"):
                 view.index("struct PhoneImageSheetView")]
    assert "sheet(item: pick)" in popup and "dismiss()" in popup
