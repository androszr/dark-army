"""One picture from a session's project, shrunk to fit one sealed answer.

The phone's Conversation tab turns an image path an agent wrote into a chip;
a tap asks the Mac for that file through the sealed `image` kind. This
module is the whole Mac half: where the file may be, and what is sent back.

**Where the file may be** (`locate`). Only inside the session's project: the
nearest folder above the session's working folder holding `.git`, or the
working folder itself when there is none — never the home folder, never
`/`, never a folder holding the home folder. A relative path is tried against the working folder, then the project
root, then (bounded, `_search`) as a tail of some file inside the project,
because an agent often names `girl/01.jpg` for `assets/girl/01.jpg`. Every
candidate is resolved, symlinks included, and must still lie inside the
project, and its folders up to the root must not pass through the home
folder or anything holding it (`_passes_home`, by identity on disk); no part
of it may be hidden (`.git`, `.env`, …); its extension
must be one of `IMAGE_EXTENSIONS`.

**What is sent back** (`render`). Never the file's own bytes, with one
exception: a GIF ImageIO itself names a GIF, animated and small enough, is
sent as it is so it still moves. Everything else is decoded by ImageIO (or,
for SVG, by AppKit, after `_svg_refusal` has turned away any SVG that would
reach for another file) and re-encoded at most `MAX_PIXELS` on its long side
and at most `PREVIEW_MAX_BYTES`, so a file named `.png` that is not a picture
is refused, not sent.

`PREVIEW_MAX_BYTES` is sized against `relay.RELAY_FRAME_MAX_BYTES`: the
bytes travel as base64 inside the reply's JSON, inside the frame, whose
inflated plaintext is capped at the same 900 000.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("dark-army")

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".heic")
#: Raw bytes of the encoded preview; base64 makes it 4/3 larger, and the
#: frame's plaintext bound is 900 000.
PREVIEW_MAX_BYTES = 450_000
#: Longest side of a preview, in pixels.
MAX_PIXELS = 2048
#: A source file larger than this is not opened at all.
SOURCE_MAX_BYTES = 60 * 1024 * 1024
SVG_MAX_BYTES = 2 * 1024 * 1024
#: A picture whose stated size is more pixels than this is not decoded: a
#: small file can claim enormous dimensions.
SOURCE_MAX_PIXELS = 100_000_000
#: An animated GIF travels as itself only while all its frames together
#: stay under this many pixels — the phone decodes every one.
GIF_MAX_TOTAL_PIXELS = 40_000_000
PATH_MAX_CHARS = 4096
#: The tail search's bounds: entries looked at, and folders never entered.
SEARCH_MAX_ENTRIES = 40_000
SEARCH_SKIP = frozenset({
    "node_modules", "DerivedData", "build", "dist", "__pycache__", "Pods",
    "venv", "target"})

KINDS_WORDS = "PNG, JPEG, GIF, SVG, WebP and HEIC pictures"
NOT_A_PICTURE = f"Dark Army shows only {KINDS_WORDS}"
OUTSIDE = ("Outside this agent's project — the Mac shows only files "
           "inside it")
HIDDEN = "Inside a hidden folder — the Mac does not show those"
MISSING = "Not there any more (moved or deleted)"
NO_PROJECT = ("This agent is not working inside a project folder, so the "
              "Mac shows none of its files")
TOO_LARGE = "Too large for the Mac to open for the phone"
UNREADABLE = "The Mac could not read this as a picture"
NOT_ONBOARDED = ("Outside Dark Army's projects — the Mac shows pictures only "
                 "from its own folder and the projects onboarded to it, "
                 "never a whole disk or the home folder")
SVG_REACHES = ("This SVG draws in other files, so the Mac does not "
               "open it for the phone")


def project_root(cwd: str, *, home: Path | None = None) -> Path | None:
    """The folder a session may show files from, or None.

    The nearest ancestor of ``cwd`` (itself included) holding `.git`,
    stopping below the home folder; otherwise ``cwd``. The home folder and
    `/` are never a project, whichever way they are reached."""
    text = str(cwd or "").strip()
    if not text or "\x00" in text or not os.path.isabs(text):
        return None
    try:
        start = Path(text).resolve()
    except (OSError, RuntimeError):
        return None
    home = (home or Path.home()).resolve()
    # A folder holding the home folder (`/Users`, a volume) is never a
    # project either: it would make every picture at home readable. Judged
    # by the folder's identity on disk, never its spelling: `/users` and
    # `/System/Volumes/Data/Users` are `/Users` on a Mac.
    held = _identities(home)
    if not start.is_dir() or _identity(start) in held:
        return None
    # The walk for `.git` stops at the home folder, or at any folder
    # holding it when reached from outside home: such a root is never used.
    probe = start
    while probe != Path(probe.anchor):
        if _identity(probe) in held:
            break
        if (probe / ".git").exists():
            return probe
        probe = probe.parent
    return start


def _onboarded() -> list:
    """Dark Army's own checkout and every enrolled project, as folder
    strings; `[]` (nothing shown) when the ledger cannot be read."""
    try:
        from . import enrollment
        found = set(enrollment.enrolled_roots())
        found.add(enrollment.self_root())
    except Exception:  # noqa: BLE001 — an unreadable ledger shows nothing
        logger.warning("picture read: could not read the onboarded projects",
                       exc_info=True)
        return []
    return sorted(r for r in found if isinstance(r, str) and r.strip())


def _in_projects(work: Path, projects, held: set,
                 home: Path | None = None) -> Path | None:
    """The onboarded folder holding ``work`` (the session's working
    folder), or None.

    Judged by identity on disk, walking up from ``work``; the walk stops at
    the home folder or any folder holding it, and an onboarded entry that
    is one of those, or a whole disk, is ignored, so no onboarding makes
    the home folder or a volume readable. Never raises."""
    allowed = set()
    for entry in projects or ():
        if not isinstance(entry, str) or "\x00" in entry \
                or not os.path.isabs(entry.strip()):
            continue
        try:
            folder = Path(entry.strip()).resolve()
            ident = _identity(folder)
            # A whole disk (`/`, `/System/Volumes/Data`, `/Volumes/X`) is
            # never a project, even onboarded: it would open everything on it.
            whole_disk = _whole_disk(folder, held, home)
        except (OSError, RuntimeError, ValueError):
            continue
        if ident is not None and ident not in held and not whole_disk:
            allowed.add(ident)
    if not allowed:
        return None
    probe = work
    while True:
        ident = _identity(probe)
        if ident is None or ident in held:
            return None
        if ident in allowed:
            return probe
        if probe == probe.parent:
            return None
        probe = probe.parent


#: Folders that are a disk's root on a Mac without being a mount point
#: `os.path.ismount` sees: the Data volume is firmlinked into `/`.
_DISK_ROOTS = (Path("/System/Volumes"), Path("/System/Volumes/Data"),
               Path("/Volumes"))


def _whole_disk(folder: Path, held: set, home: Path | None = None) -> bool:
    """True for a disk's root, a volume under `/Volumes`, or any folder
    holding the home folder under another name (`<folder>/Users/<me>` is
    home), none of which is ever a project."""
    if os.path.ismount(folder) or folder in _DISK_ROOTS \
            or folder.parent == Path("/Volumes"):
        return True
    home_parts = (home or Path.home()).resolve().parts[1:]
    for k in range(1, len(home_parts) + 1):
        if _identity(folder.joinpath(*home_parts[-k:])) in held:
            return True
    return False


def _identity(folder: Path):
    try:
        st = os.stat(folder)
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino)


def _identities(home: Path) -> set:
    """The on-disk identity of the home folder and of every folder holding
    it, up to `/`."""
    out = set()
    probe = home
    while True:
        ident = _identity(probe)
        if ident is not None:
            out.add(ident)
        if probe == probe.parent:
            return out
        probe = probe.parent


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _hidden(path: Path, root: Path) -> bool:
    return any(part.startswith(".") for part in path.relative_to(root).parts)


def _passes_home(real: Path, root: Path, held: set) -> bool:
    """Whether a file's folders, from its own up to ``root``, pass through
    the home folder or any folder holding it — judged by identity on disk.
    A project inside home never meets one; a root that holds home under
    another name (`/System/Volumes/Data`, a mount) always does."""
    probe = real.parent
    while True:
        if _identity(probe) in held:
            return True
        if probe == root or probe == probe.parent:
            return False
        probe = probe.parent


def _search(root: Path, tail: tuple, limit: int = SEARCH_MAX_ENTRIES,
            held: set = frozenset()):
    """Files inside ``root`` whose last parts are ``tail``, newest first.
    Hidden folders and `SEARCH_SKIP` are never entered; symlinked folders
    are not followed; at most ``limit`` entries are looked at, counted as
    the folder is read, never after loading it whole."""
    found = []
    seen = 0
    stack = [root]
    name = tail[-1]
    while stack and seen < limit:
        folder = stack.pop()
        try:
            entries = os.scandir(folder)
        except OSError:
            continue
        with entries:
            for entry in entries:
                seen += 1
                if seen > limit:
                    break
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in SEARCH_SKIP and (
                                not held
                                or _identity(Path(entry.path)) not in held):
                            stack.append(Path(entry.path))
                    elif entry.name == name \
                            and entry.is_file(follow_symlinks=False):
                        candidate = Path(entry.path)
                        if candidate.parts[-len(tail):] == tail:
                            found.append(candidate)
                except OSError:
                    continue

    def _mtime(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    return sorted(found, key=_mtime, reverse=True)


def locate(cwd: str, path: str, *, home: Path | None = None,
           projects=None):
    """``(file, root, "")`` for a path the session may show, or
    ``(None, None, reason)`` in words.

    ``projects`` is the onboarded folders the root must lie in; `None`
    reads them from the enrolment ledger (`_onboarded`)."""
    text = str(path or "").strip()
    if not text or len(text) > PATH_MAX_CHARS or "\x00" in text:
        return None, None, MISSING
    if text.startswith("file://"):
        text = text[len("file://"):]
    if Path(text).suffix.lower() not in IMAGE_EXTENSIONS:
        return None, None, NOT_A_PICTURE
    root = project_root(cwd, home=home)
    if root is None:
        return None, None, NO_PROJECT
    held = _identities((home or Path.home()).resolve())
    work = Path(str(cwd)).resolve()
    # The working folder must lie in an onboarded project; the root is then
    # the nearer of the git root and that project, so an onboarded
    # subfolder of a bigger repository opens only itself.
    onboarded = _in_projects(work, _onboarded() if projects is None
                             else projects, held, home)
    if onboarded is None:
        return None, None, NOT_ONBOARDED
    if not _inside(root, onboarded):
        root = onboarded
    given = Path(os.path.expanduser(text)) if text.startswith("~") \
        else Path(text)
    if given.is_absolute():
        candidates = [given]
    else:
        candidates = [work / given, root / given]
    refusal = MISSING
    for candidate in candidates:
        # A spelled `..` walking out is refused even when it walks back in.
        # Then the resolved path: a symlink may not leave. (Only a `..` is
        # judged as written: an absolute path through a symlinked folder,
        # `/tmp` for `/private/tmp`, is judged where it really leads.)
        spelled = Path(os.path.normpath(str(candidate)))
        if ".." in candidate.parts and not _inside(spelled, root):
            refusal = OUTSIDE
            continue
        try:
            real = candidate.resolve()
        except (OSError, RuntimeError):
            continue
        if not _inside(real, root):
            refusal = OUTSIDE
            continue
        if _hidden(real, root) or (_inside(spelled, root)
                                    and _hidden(spelled, root)):
            refusal = HIDDEN
            continue
        if real.suffix.lower() not in IMAGE_EXTENSIONS:
            refusal = NOT_A_PICTURE
            continue
        if _passes_home(real, root, held):
            refusal = OUTSIDE
            continue
        if real.is_file():
            return real, root, ""
    if refusal == MISSING and not given.is_absolute() \
            and ".." not in given.parts \
            and not any(p.startswith(".") for p in given.parts):
        for hit in _search(root, given.parts, held=held):
            try:
                real = hit.resolve()
            except (OSError, RuntimeError):
                continue
            if _inside(real, root) and not _hidden(real, root) \
                    and real.suffix.lower() in IMAGE_EXTENSIONS \
                    and not _passes_home(real, root, held) \
                    and real.is_file():
                return real, root, ""
    return None, None, refusal


_SVG_REACH = re.compile(
    rb"<!ENTITY|<!DOCTYPE[^>]*\[|@import|<foreignObject"
    rb"|(?:href|src)\s*=\s*(?![\"']?\s*(?:#|data:))"
    rb"|url\(\s*[\"']?(?!\s*(?:#|data:))",
    re.IGNORECASE)


def _svg_refusal(data: bytes) -> str:
    """``SVG_REACHES`` for an SVG that names anything but itself or inline
    data — an external image, a stylesheet, an entity — else ``""``. The
    match reads UTF-8 text, so anything else (UTF-16, a byte-order mark, a
    NUL, a declared other encoding) is refused rather than matched past."""
    if b"\x00" in data or data.startswith((b"\xef\xbb\xbf", b"\xff\xfe",
                                            b"\xfe\xff")):
        return SVG_REACHES
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return SVG_REACHES
    declared = re.search(rb"<\?xml[^>]*encoding\s*=\s*[\"']([^\"']+)", data,
                         re.IGNORECASE)
    if declared and declared.group(1).lower().replace(b"-", b"") \
            not in (b"utf8", b"usascii", b"ascii"):
        return SVG_REACHES
    return SVG_REACHES if _SVG_REACH.search(data) else ""


def _encode(image, fmt: str, quality: float):
    """One CGImage as ``fmt`` bytes, or None."""
    import Quartz
    from Foundation import NSMutableData
    uti = {"jpeg": "public.jpeg", "png": "public.png"}[fmt]
    out = NSMutableData.data()
    dest = Quartz.CGImageDestinationCreateWithData(out, uti, 1, None)
    if dest is None:
        return None
    props = {Quartz.kCGImageDestinationLossyCompressionQuality: quality} \
        if fmt == "jpeg" else None
    Quartz.CGImageDestinationAddImage(dest, image, props)
    if not Quartz.CGImageDestinationFinalize(dest):
        return None
    return bytes(out)


def _has_alpha(image) -> bool:
    import Quartz
    info = Quartz.CGImageGetAlphaInfo(image)
    return info not in (Quartz.kCGImageAlphaNone,
                        Quartz.kCGImageAlphaNoneSkipLast,
                        Quartz.kCGImageAlphaNoneSkipFirst)


def _fit(make, alpha: bool):
    """``(bytes, fmt, width, height)`` from ``make(max_pixels)`` (a CGImage
    at most that long), stepping the size and format down until it fits
    `PREVIEW_MAX_BYTES`; None when nothing fits."""
    import Quartz
    # A picture with transparency keeps it down to 1024 pixels as PNG
    # before JPEG, which has none, is tried at all.
    ladder = ([(side, "png", 1.0) for side in (MAX_PIXELS, 1600, 1280, 1024)]
              if alpha else [])
    for side in (MAX_PIXELS, 1600, 1280, 1024, 800, 640):
        ladder += [(side, "jpeg", 0.8), (side, "jpeg", 0.6)]
    made = {}
    for side, fmt, quality in ladder:
        if side not in made:
            made[side] = make(side)
        image = made[side]
        if image is None:
            return None
        data = _encode(image, fmt, quality)
        if data and len(data) <= PREVIEW_MAX_BYTES:
            return (data, fmt, int(Quartz.CGImageGetWidth(image)),
                    int(Quartz.CGImageGetHeight(image)))
    return None


def _raster(file: Path, info: dict) -> dict:
    import Quartz
    from Foundation import NSURL
    source = Quartz.CGImageSourceCreateWithURL(
        NSURL.fileURLWithPath_(str(file)), None)
    if source is None or Quartz.CGImageSourceGetCount(source) < 1 \
            or Quartz.CGImageSourceGetType(source) is None:
        return _refused(info, UNREADABLE)
    kind = str(Quartz.CGImageSourceGetType(source))
    frames = int(Quartz.CGImageSourceGetCount(source))
    props = Quartz.CGImageSourceCopyPropertiesAtIndex(source, 0, None) or {}
    width = int(props.get(Quartz.kCGImagePropertyPixelWidth) or 0)
    height = int(props.get(Quartz.kCGImagePropertyPixelHeight) or 0)
    orientation = int(props.get(Quartz.kCGImagePropertyOrientation) or 1)
    if orientation in (5, 6, 7, 8):
        width, height = height, width
    info.update(width=width, height=height,
                animated=kind == "com.compuserve.gif" and frames > 1,
                frames=frames if kind == "com.compuserve.gif" else 1)
    if width <= 0 or height <= 0:
        return _refused(info, UNREADABLE)
    if width * height > SOURCE_MAX_PIXELS:
        return _refused(info, TOO_LARGE)
    if info["animated"] and info["bytes"] <= PREVIEW_MAX_BYTES \
            and width * height * frames <= GIF_MAX_TOTAL_PIXELS:
        # The one case the file's own bytes travel: ImageIO named it a GIF.
        info.update(format="gif", data=base64.b64encode(
            file.read_bytes()).decode("ascii"),
            shown_width=width, shown_height=height, reduced=False)
        return info

    def make(side):
        return Quartz.CGImageSourceCreateThumbnailAtIndex(source, 0, {
            Quartz.kCGImageSourceCreateThumbnailFromImageAlways: True,
            Quartz.kCGImageSourceCreateThumbnailWithTransform: True,
            Quartz.kCGImageSourceThumbnailMaxPixelSize: side,
            Quartz.kCGImageSourceShouldCacheImmediately: True,
        })

    first = make(64)
    if first is None:
        return _refused(info, UNREADABLE)
    alpha = bool(props.get(Quartz.kCGImagePropertyHasAlpha)) \
        if Quartz.kCGImagePropertyHasAlpha in props else _has_alpha(first)
    fitted = _fit(make, alpha)
    if fitted is None:
        return _refused(info, TOO_LARGE)
    return _accepted(info, *fitted)


def _svg(file: Path, info: dict) -> dict:
    if info["bytes"] > SVG_MAX_BYTES:
        return _refused(info, TOO_LARGE)
    data = file.read_bytes()
    refusal = _svg_refusal(data)
    if refusal:
        return _refused(info, refusal)
    import AppKit
    import Quartz
    from Foundation import NSData
    picture = AppKit.NSImage.alloc().initWithData_(
        NSData.dataWithBytes_length_(data, len(data)))
    if picture is None:
        return _refused(info, UNREADABLE)
    size = picture.size()
    w, h = float(size.width), float(size.height)
    if w <= 0 or h <= 0:
        return _refused(info, UNREADABLE)
    info.update(width=int(round(w)), height=int(round(h)))

    def make(side):
        # Drawn at the size asked for, never smaller than the SVG says:
        # a vector picture is sharp at any size up to the cap.
        scale = side / max(w, h)
        pw, ph = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
        space = Quartz.CGColorSpaceCreateDeviceRGB()
        ctx = Quartz.CGBitmapContextCreate(
            None, pw, ph, 8, 0, space,
            Quartz.kCGImageAlphaPremultipliedLast)
        if ctx is None:
            return None
        graphics = AppKit.NSGraphicsContext.graphicsContextWithCGContext_flipped_(
            ctx, False)
        AppKit.NSGraphicsContext.saveGraphicsState()
        try:
            AppKit.NSGraphicsContext.setCurrentContext_(graphics)
            picture.drawInRect_(((0, 0), (pw, ph)))
        finally:
            AppKit.NSGraphicsContext.restoreGraphicsState()
        return Quartz.CGBitmapContextCreateImage(ctx)

    fitted = _fit(make, True)
    if fitted is None:
        return _refused(info, TOO_LARGE)
    return _accepted(info, *fitted)


def _refused(info: dict, reason: str) -> dict:
    return {k: v for k, v in info.items() if k != "data"} | {
        "available": False, "reason": reason}


def _accepted(info: dict, data: bytes, fmt: str, width: int,
              height: int) -> dict:
    info.update(format=fmt, data=base64.b64encode(data).decode("ascii"),
                shown_width=width, shown_height=height,
                reduced=(width, height) != (info.get("width"),
                                            info.get("height"))
                or fmt != info.get("source_format"))
    return info


def render(file: Path, root: Path) -> dict:
    """The answer for one located file (`locate`'s ``file``)."""
    try:
        stat = file.stat()
    except OSError:
        return {"available": False, "path": "", "reason": MISSING}
    suffix = file.suffix.lower()
    info = {
        "available": True,
        "path": str(file.relative_to(root)),
        "name": file.name,
        "bytes": int(stat.st_size),
        "modified": float(stat.st_mtime),
        "source_format": {".jpg": "jpeg"}.get(suffix, suffix.lstrip(".")),
        "width": 0, "height": 0, "animated": False, "frames": 1,
    }
    if stat.st_size > SOURCE_MAX_BYTES:
        return _refused(info, TOO_LARGE)
    try:
        if suffix == ".svg":
            return _svg(file, info)
        return _raster(file, info)
    except Exception:  # noqa: BLE001 — any image-library failure is a refusal
        return _refused(info, UNREADABLE)


def preview(cwd: str, path: str, *, home: Path | None = None,
            projects=None) -> dict:
    """The whole read: `locate`, then `render`. Blocking; run it off the
    loop."""
    file, root, reason = locate(cwd, path, home=home, projects=projects)
    if file is None:
        return {"available": False, "path": str(path or "")[:PATH_MAX_CHARS],
                "reason": reason}
    # An executor thread never drains an autorelease pool of its own: every
    # Cocoa object `render` makes would outlive it (tens of MB a picture).
    import objc
    with objc.autorelease_pool():
        return render(file, root)
