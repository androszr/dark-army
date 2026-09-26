"""Posting an alert to macOS — the last mile `alerts.py` deliberately left out.

`alerts.AlertPolicy` decides *whether* something has earned an interruption;
this posts it. The split is the same one the daemon draws everywhere else: the
policy is pure and testable with a dict and a clock, and the part that talks to
AppKit is small, guarded, and does no thinking.

**Why this lives in the menu-bar process, against what the comments used to say.**
`daemon.py` and `alerts.py` both named the Swift panel as the eventual deliverer.
That is impossible: `UNUserNotificationCenter.current()` traps without a bundle
identity, and the panel binary sits at `Contents/Resources/BobPanel` — measured,
`Bundle.main.bundleIdentifier` is `nil` there, because CFBundle only walks up
from `Contents/MacOS`. The Python menu-bar process *is* the bundle
(`com.bob-companion.menubar`), so it is the only thing in this app that can post.

**No new dependency.** `objc.loadBundle` pulls the framework's classes in at
runtime, so there is no `pyobjc-framework-UserNotifications` for py2app to
freeze — one less thing that can fail to freeze, which is the same reason the
API server is hand-rolled. The cost is that PyObjC has no metadata for the two
selectors that take blocks, so their signatures are declared by hand below.

**Signing.** A Developer ID is *not* required: an ad-hoc signature is enough for
the authorization prompt and for banners — verified on this machine, which has no
signing identity at all. Action buttons (the category registered below) are the
part reported to fail silently without a real identity, so nothing here depends
on them: the body click opens Dark Army's panel on that agent, and everything the
buttons offer — the editor, dismissing the card, muting — is a press away there.

Everything is best-effort. A Mac that refuses notifications, a framework that
will not load, a user who said no — none of it may take the menu bar down, and
none of it is worth more than a line in the log. Silence is the failure mode.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("dark-army.notifier")

FRAMEWORK_PATH = "/System/Library/Frameworks/UserNotifications.framework"

# The avatar drawn on the banner. Sized for the notification thumbnail slot,
# which is small and fixed — 256pt is comfortably above what any display asks
# for and the portrait is scaled to it once, not per banner.
AVATAR_PT = 256.0
# The disc the portrait is clipped to, matching the panel's `AvatarDisc`: one
# footprint, whatever the photograph's framing. Off-white, because a portrait
# that has not been chosen yet leaves the disc bare and a notification is
# composited over whatever the system's material happens to be that year.
_DISC_WHITE = 0.94

# The one category, carrying the verbs an alert can offer. Its identifier is
# stored on every request; macOS matches them by string.
CATEGORY_ID = "bob.alert"
# Which banners hang a portrait. The same three words as `alerts.FACE_KINDS`,
# restated here so this process does not import the daemon. Attention and
# security stay faceless; `post_notice` never reaches `_attach_avatar`.
# `test_phone_buzz_kinds.py` pins the two tuples equal.
FACE_KINDS = ("question", "permission", "finished")
# Tapping the banner's body. Opens Dark Army's own panel on that agent rather than its
# editor: the banner says an agent needs you and gives four seconds of it, and
# the panel is where the rest is — what it asked, what it costs, and the buttons
# that answer it without typing. The editor is one press away from there (Enter,
# or the ↗ chip), where from a banner in the editor the panel is not.
ACTION_OPEN = "open"
ACTION_REVEAL = "reveal"
ACTION_DISMISS = "dismiss"
ACTION_MUTE = "mute"

# UNAuthorizationOptions. Declared rather than imported for the usual reason:
# dynamic loading gives us classes, not constants.
_OPTION_BADGE = 1 << 0
_OPTION_SOUND = 1 << 1
_OPTION_ALERT = 1 << 2

# UNNotificationPresentationOptions, for the rare tick where our own app is
# frontmost (the panel takes key when it opens). Banner + sound, i.e. behave
# exactly as it would when we are in the background.
_PRESENT_BANNER = 1 << 4
_PRESENT_SOUND = 1 << 1

# UNAuthorizationStatus, by value. `provisional` (3) and `ephemeral` (4) can
# both post, so they are read as authorized; the point of the read is to tell
# an explicit **denied** from everything that is not one.
_STATUS_NOT_DETERMINED = 0
_STATUS_DENIED = 1
_STATUS_AUTHORIZED = 2

#: The words the panel is given. `unavailable` is a Mac with no bundle
#: identity or no framework — it is *not* denied and draws no warning;
#: `error` is a query that raised or a callback that never came; `unknown`
#: is "not asked yet".
STATUS_AUTHORIZED = "authorized"
STATUS_DENIED = "denied"
STATUS_NOT_DETERMINED = "not_determined"
STATUS_UNAVAILABLE = "unavailable"
STATUS_ERROR = "error"
STATUS_UNKNOWN = "unknown"
STATUSES = (STATUS_AUTHORIZED, STATUS_DENIED, STATUS_NOT_DETERMINED,
            STATUS_UNAVAILABLE, STATUS_ERROR, STATUS_UNKNOWN)

#: How long one settings read may stay "in flight" before a fresh
#: `refresh_status` is allowed to ask again. The framework answers in
#: milliseconds; a completion that never comes must not coalesce every later
#: return onto it for the life of the process, or a denied warning could
#: never clear.
STATUS_INFLIGHT_TIMEOUT_SECONDS = 10.0


def authorization_status_name(raw) -> str:
    """Map a `UNAuthorizationStatus` integer onto the published words.

    Pure, so the table is testable without the framework: 0 is not
    determined, 1 denied, 2/3/4 authorized (full, provisional, ephemeral).
    Anything else — a bool, a string, a future value — is `error`, never
    denied: a warning is drawn only for an answer that actually said no.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return STATUS_ERROR
    if raw == _STATUS_NOT_DETERMINED:
        return STATUS_NOT_DETERMINED
    if raw == _STATUS_DENIED:
        return STATUS_DENIED
    if raw >= _STATUS_AUTHORIZED and raw <= 4:
        return STATUS_AUTHORIZED
    return STATUS_ERROR


# UNNotificationActionOptions.foreground — bring the app forward when tapped.
_ACTION_FOREGROUND = 1 << 2
# .destructive, for Dismiss: it ends the card.
_ACTION_DESTRUCTIVE = 1 << 1


def _load_framework() -> Optional[dict]:
    """Bring UserNotifications in at runtime, or None if it cannot be had."""
    try:
        import objc
    except ImportError:                      # pragma: no cover - PyObjC missing
        return None
    namespace: dict = {}
    try:
        objc.loadBundle("UserNotifications", namespace,
                        bundle_path=FRAMEWORK_PATH)
    except Exception:
        logger.info("UserNotifications did not load; no banners", exc_info=True)
        return None
    if "UNUserNotificationCenter" not in namespace:
        return None
    _register_block_signatures(objc)
    return namespace


def _register_block_signatures(objc) -> None:
    """Teach PyObjC the two block-taking selectors we call.

    Without metadata a block argument is just an unknown pointer and the call
    raises. These two are the only ones we invoke; the delegate methods we
    *implement* declare their own signatures at the selector instead.
    """
    try:
        objc.registerMetaDataForSelector(
            b"UNUserNotificationCenter",
            b"requestAuthorizationWithOptions:completionHandler:",
            {"arguments": {3: {"callable": {
                "retval": {"type": b"v"},
                "arguments": {0: {"type": b"^v"},
                              1: {"type": objc._C_BOOL},
                              2: {"type": b"@"}}}}}})
        objc.registerMetaDataForSelector(
            b"UNUserNotificationCenter",
            b"addNotificationRequest:withCompletionHandler:",
            {"arguments": {3: {"callable": {
                "retval": {"type": b"v"},
                "arguments": {0: {"type": b"^v"}, 1: {"type": b"@"}}}}}})
        # The settings read: one block argument carrying the
        # UNNotificationSettings object. Same discipline as the two above.
        objc.registerMetaDataForSelector(
            b"UNUserNotificationCenter",
            b"getNotificationSettingsWithCompletionHandler:",
            {"arguments": {2: {"callable": {
                "retval": {"type": b"v"},
                "arguments": {0: {"type": b"^v"}, 1: {"type": b"@"}}}}}})
    except Exception:                        # pragma: no cover - PyObjC internals
        logger.debug("Could not register block signatures", exc_info=True)


def bundle_identity() -> str:
    """Our bundle id, or "" when running from source.

    The gate on everything below. A dev run (`python -m dark_army_menubar`)
    has no bundle, and asking the notification centre for one is not a
    degraded experience but a crash.
    """
    try:
        from Foundation import NSBundle
        return NSBundle.mainBundle().bundleIdentifier() or ""
    except Exception:
        return ""


def _portrait_dir() -> Optional[Path]:
    """Where the photo portraits live, frozen bundle first then a checkout.

    The frozen path is inside the *panel's* SwiftPM resource bundle. That is not
    a layering violation so much as an admission: `build.sh` already copies that
    bundle in, it is the same picture the panel draws, and shipping a second
    copy for Python would guarantee the two drift. The menu-bar strip's pixel
    art (`assets/cast`) is a different tree and is never read here: a banner
    draws a face at a size where a photograph is the right kind of picture.
    """
    try:
        from Foundation import NSBundle
        bundle = NSBundle.mainBundle()
        if bundle and bundle.bundlePath():
            cand = (Path(bundle.bundlePath()) / "Contents" / "Resources"
                    / "BobPanel_BobPanel.bundle" / "portraits")
            if cand.is_dir():
                return cand
    except Exception:
        pass
    # A source checkout: host/dark_army_menubar/notifier.py -> repo root is
    # three up, and the portraits' source of truth is assets/portraits.
    here = Path(os.path.realpath(__file__))
    for _ in range(3):
        here = here.parent
    cand = here / "assets" / "portraits"
    return cand if cand.is_dir() else None


def _portrait_path(character: str) -> Optional[Path]:
    """One character's still, or None where the tree has none yet."""
    root = _portrait_dir()
    if not root or not character:
        return None
    path = root / f"{character.lower()}.png"
    return path if path.is_file() else None


#: composed avatars, keyed by character slug. Decoding a 512px PNG and
#: compositing it onto a disc is work done once per character per process
#: rather than per banner, on the main thread, at the moment something is
#: already urgent.
_avatar_cache: dict = {}


def _avatar_png(character: str) -> Optional[str]:
    """Render the character's portrait onto a disc and return a PNG path, or None.

    Scaled **down** from the 512px portrait with interpolation **on**. The old
    integer-factor / interpolation-off rule was a pixel-art rule — right for the
    strip's hand-drawn figures, wrong for a photograph, which turns to a grid
    of blocks under nearest-neighbour. The portrait is clipped to the disc so
    the banner's face has the same footprint as the panel's.

    A character with no portrait yet is **no face** rather than somebody
    else's: the miss is cached as ``""`` and the banner goes out faceless.
    """
    key = (character or "").lower()
    if key in _avatar_cache:
        return _avatar_cache[key] or None
    portrait = _portrait_path(key)
    if portrait is None:
        _avatar_cache[key] = ""
        return None
    try:
        from AppKit import (NSBitmapImageRep, NSGraphicsContext, NSImage,
                            NSColor, NSBezierPath, NSCalibratedRGBColorSpace)

        size = int(AVATAR_PT)
        rep = NSBitmapImageRep.alloc() \
            .initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
                None, size, size, 8, 4, True, False,
                NSCalibratedRGBColorSpace, 0, 0)
        ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
        if rep is None or ctx is None:
            raise RuntimeError("no bitmap context")
        NSGraphicsContext.saveGraphicsState()
        try:
            NSGraphicsContext.setCurrentContext_(ctx)
            ctx.setImageInterpolation_(3)          # NSImageInterpolationHigh
            disc = NSBezierPath.bezierPathWithOvalInRect_(
                ((0.0, 0.0), (float(size), float(size))))
            NSColor.colorWithCalibratedWhite_alpha_(_DISC_WHITE, 1.0).setFill()
            disc.fill()
            disc.addClip()

            image = NSImage.alloc().initWithContentsOfFile_(str(portrait))
            if image is None:
                raise RuntimeError(f"could not read {portrait}")
            src = image.size()
            # Fill the disc: scale so the shorter side spans it, centre the
            # longer one, and let the clip take the corners.
            scale = size / max(1.0, min(src.width, src.height))
            w, h = src.width * scale, src.height * scale
            image.drawInRect_fromRect_operation_fraction_(
                (((size - w) / 2.0, (size - h) / 2.0), (w, h)),
                ((0.0, 0.0), (0.0, 0.0)), 2, 1.0)  # NSCompositingOperationSourceOver
        finally:
            NSGraphicsContext.restoreGraphicsState()

        data = rep.representationUsingType_properties_(4, {})   # PNG
        out = Path(tempfile.gettempdir()) / "dark-army-avatars"
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{key}.png"
        if not data.writeToFile_atomically_(str(path), True):
            raise RuntimeError(f"could not write {path}")
        _avatar_cache[key] = str(path)
        return str(path)
    except Exception:
        # A face is a nicety; a banner without one still says everything it has
        # to say. Never let this path be the reason an interruption is lost.
        logger.debug("Could not compose the avatar for %s", key, exc_info=True)
        _avatar_cache[key] = ""
        return None


def _unpack_result(result):
    """`(value, error)` from a factory that may or may not return the pair.

    See `_attach_avatar`: whether an `NSError **` comes back as a second return
    value depends on PyObjC having metadata for the selector, and a class loaded
    at runtime has none. A tuple is unpacked; anything else *is* the value.
    """
    if isinstance(result, tuple) and len(result) == 2:
        return result
    return result, None


class Notifier:
    """Posts alerts, remembers whether it is allowed to, and routes the taps.

    Construction is cheap and never raises: `available` is False when anything
    is missing, and every method is a no-op in that state. Nothing calls into
    AppKit until `start()`.
    """

    def __init__(self, on_action: Optional[Callable[[str, str], None]] = None,
                 on_status: Optional[Callable[[str], None]] = None):
        #: called with (action, session_id) when the user taps something
        self._on_action = on_action
        #: called with one of `STATUSES` whenever an explicit settings read
        #: answers — from the framework's thread, so the receiver hops.
        self._on_status = on_status
        self._ns = _load_framework() if bundle_identity() else None
        self._center = None
        self._delegate = None
        self._authorized: Optional[bool] = None    # None = not asked yet
        #: The last explicit authorization status read back from the
        #: centre, in `STATUSES`' words. `unknown` until `refresh_status`
        #: has answered once.
        self._status: str = STATUS_UNKNOWN
        #: One settings read in flight at a time: a burst of panel returns
        #: coalesces onto the first, whose callback answers them all.
        self._status_inflight = False
        self._status_inflight_at = 0.0
        self._status_waiters: list = []
        if self._ns is None:
            return
        try:
            self._center = self._ns["UNUserNotificationCenter"] \
                .currentNotificationCenter()
        except Exception:
            logger.info("No notification centre available", exc_info=True)
            self._center = None

    @property
    def available(self) -> bool:
        """Whether posting is even possible — not whether it is permitted."""
        return self._center is not None

    @property
    def authorized(self) -> Optional[bool]:
        """True, False, or None while the user has not answered yet."""
        return self._authorized

    @property
    def status(self) -> str:
        """The last authorization status read, or `unavailable` where
        nothing here can be asked."""
        if not self.available:
            return STATUS_UNAVAILABLE
        return self._status

    # ── the explicit status read ─────────────────────────────────────────────
    def refresh_status(self, on_status: Optional[Callable[[str], None]] = None) -> bool:
        """Ask the centre for its current authorization status, asynchronously.

        `on_status(status)` is called **from the framework's callback thread**
        with one of `STATUSES`; the caller hops to the main thread itself. A
        read already in flight takes the new callback onto the same answer
        rather than starting a second query. Returns whether a read is now
        pending. Never asks for authorization: this is a read of what the
        person already decided, and a Mac with nothing to ask answers
        `unavailable` at once, on the caller's thread.
        """
        if not self.available:
            if on_status is not None:
                on_status(STATUS_UNAVAILABLE)
            return False
        if on_status is not None:
            self._status_waiters.append(on_status)
        now = time.monotonic()
        if self._status_inflight:
            age = now - getattr(self, "_status_inflight_at", 0.0)
            if age < STATUS_INFLIGHT_TIMEOUT_SECONDS:
                return True
            logger.info("Notification settings read has hung for %.0fs; "
                        "asking again", age)
        self._status_inflight = True
        self._status_inflight_at = now
        try:
            self._center.getNotificationSettingsWithCompletionHandler_(
                self._settings_cb)
        except Exception:
            logger.info("Could not read notification settings", exc_info=True)
            self._deliver_status(STATUS_ERROR)
            return False
        return True

    def _settings_cb(self, settings) -> None:
        """The framework's answer. `settings` is a `UNNotificationSettings`;
        anything that will not give an integer status is an `error`."""
        try:
            raw = settings.authorizationStatus() if settings is not None else None
        except Exception:
            logger.debug("Notification settings object was not readable",
                         exc_info=True)
            raw = None
        self._deliver_status(authorization_status_name(raw))

    def _deliver_status(self, status: str) -> None:
        self._status = status
        # A denial read back is the one answer that also settles `post`'s
        # gate; an authorized read re-opens it. Anything else is left as
        # the request callback set it.
        if status == STATUS_DENIED:
            self._authorized = False
        elif status == STATUS_AUTHORIZED:
            self._authorized = True
        waiters, self._status_waiters = self._status_waiters, []
        self._status_inflight = False
        standing = getattr(self, "_on_status", None)
        if standing is not None:
            waiters = [standing] + waiters
        for cb in waiters:
            try:
                cb(status)
            except Exception:
                logger.debug("Notification status callback failed", exc_info=True)

    # ── setup ────────────────────────────────────────────────────────────────
    def start(self) -> None:
        """Ask for permission and register the category. Main thread.

        The prompt appears once per install, ever; macOS remembers the answer in
        Notification Center's own preferences and never asks again, so this is
        safe to call on every launch.
        """
        if not self.available:
            return
        self._install_delegate()
        self._register_category()
        try:
            self._center.requestAuthorizationWithOptions_completionHandler_(
                _OPTION_ALERT | _OPTION_SOUND | _OPTION_BADGE, self._authorized_cb)
        except Exception:
            logger.info("Could not request notification authorization",
                        exc_info=True)

    def _authorized_cb(self, granted, error) -> None:
        self._authorized = bool(granted)
        if granted:
            logger.info("Notifications authorized")
        else:
            # Not a warning: "no" is a legitimate answer, and the app works
            # without it — the strip and the menu still show the same state.
            logger.info("Notifications not authorized (%s); "
                        "alerts stay in the menu bar only", error)
        # `granted` false folds a denial and a framework error into one
        # Boolean. The explicit settings read that follows is what tells them
        # apart, and it is the only thing that may draw the denied warning.
        try:
            self.refresh_status()
        except Exception:
            logger.debug("Could not follow the request with a status read",
                         exc_info=True)

    def _register_category(self) -> None:
        """The three buttons. Best-effort by design — see the module docstring:
        categories are the part that goes quiet without a real signing identity,
        and a banner with no buttons is still the notification."""
        try:
            Action = self._ns["UNNotificationAction"]
            Category = self._ns["UNNotificationCategory"]
            actions = [
                Action.actionWithIdentifier_title_options_(
                    ACTION_REVEAL, "Open in Editor", _ACTION_FOREGROUND),
                Action.actionWithIdentifier_title_options_(
                    ACTION_DISMISS, "Dismiss", _ACTION_DESTRUCTIVE),
                Action.actionWithIdentifier_title_options_(
                    ACTION_MUTE, "Mute", 0),
            ]
            category = Category.categoryWithIdentifier_actions_intentIdentifiers_options_(
                CATEGORY_ID, actions, [], 0)
            self._center.setNotificationCategories_({category})
        except Exception:
            logger.debug("Could not register the alert category", exc_info=True)

    def _install_delegate(self) -> None:
        """Route taps back to us. Without a delegate a click just opens the app."""
        try:
            self._delegate = _make_delegate(self._handle_response)
            self._center.setDelegate_(self._delegate)
        except Exception:
            logger.debug("Could not install the notification delegate",
                         exc_info=True)

    # ── posting ──────────────────────────────────────────────────────────────
    def post(self, alert: dict) -> bool:
        """Post one alert. Returns whether it was handed to macOS.

        The alert's own id is the request identifier, so the same interruption
        re-posted (a restart mid-flight, say) replaces its banner instead of
        stacking a second one.
        """
        if not self.available or self._authorized is False:
            return False
        title = str(alert.get("title") or "An agent needs you")
        subtitle = str(alert.get("subtitle") or "")
        # A banner with an empty body is rendered by macOS as a bare "Notification"
        # — the app name and nothing else. Every alert here has something to say,
        # so an empty body means the sentence was lost upstream; say the least
        # wrong thing rather than let the system fill the gap with a placeholder.
        body = str(alert.get("body") or "").strip() or "Waiting for you"
        session = str(alert.get("session_id") or "")
        try:
            content = self._ns["UNMutableNotificationContent"].alloc().init()
            content.setTitle_(title)
            if subtitle:
                content.setSubtitle_(subtitle)
            content.setBody_(body)
            content.setCategoryIdentifier_(CATEGORY_ID)
            # Group by agent, so a session that interrupts twice stacks under
            # itself instead of pushing the other agents' banners out of the way.
            if session:
                content.setThreadIdentifier_(session)
            # The session id rides along in userInfo rather than being parsed
            # back out of the request identifier, which is a composite
            # (`<sid>:<rule>:<n>`) and would have to be split on a character
            # session ids are not guaranteed to lack.
            content.setUserInfo_({"session_id": session})
            # A finished run is news, not a summons: its banner arrives
            # silently (`alerts.REPORT_RULE`, and the older tldr-plus-report
            # card of the same kind). Every other kind keeps the sound.
            if alert.get("kind") != "finished":
                content.setSound_(self._ns["UNNotificationSound"].defaultSound())
            self._attach_avatar(content, alert)
            request = self._ns["UNNotificationRequest"] \
                .requestWithIdentifier_content_trigger_(
                    str(alert.get("id") or session or title), content, None)
            self._center.addNotificationRequest_withCompletionHandler_(
                request, self._added_cb)
            # Logged at info, and with the text: a banner is gone in four
            # seconds, and "did it fire and I missed it, or did it never fire"
            # is otherwise unanswerable after the fact.
            logger.info("Banner: %r / %r / %r", title, subtitle, body)
            return True
        except Exception:
            logger.debug("Could not post a notification", exc_info=True)
            return False

    def _attach_avatar(self, content, alert: dict) -> None:
        """Put the agent's face on the banner. Best-effort, like everything here.

        The thumbnail slot, not the icon on the left: that one is the app's, and
        replacing it needs Communication Notifications — an `INSendMessageIntent`
        and the `usernotifications.communication` entitlement, which needs a real
        signing identity. This does the job the cast exists for anyway, which is
        to say *who* before you have read a word.

        The file is **copied** per post because `UNNotificationAttachment` moves
        what it is given into its own store. Handing it the cached render would
        work exactly once and leave every later banner faceless.

        The factory's `error:` parameter is *not* an out-parameter here. On a
        class PyObjC has metadata for it would be, and the call would return
        `(attachment, error)`; on one pulled in by `objc.loadBundle` there is no
        metadata, so it returns the attachment alone. Unpacking a pair from that
        raises, the `except` below eats it, and every banner arrives with no face
        and no complaint — which is exactly what the first version of this did.
        Both shapes are accepted rather than guessed at.

        A portrait hangs only for a question, a permission ask or a finished
        run (`FACE_KINDS`). A plain needs-you and a machine warning do not.
        """
        if str(alert.get("kind") or "") not in FACE_KINDS:
            return
        character = str(alert.get("character") or "")
        source = _avatar_png(character) if character else None
        if not source:
            return
        try:
            from Foundation import NSURL
            # One directory, not one per banner. `mkdtemp` here leaked a
            # directory every time an agent needed someone — and the file
            # cannot simply be deleted after the call, because the attachment
            # store *moves* it. A stable path per character is bounded
            # (fifteen faces) and re-copied whenever the store has taken
            # the last one away.
            tmp = os.path.join(tempfile.gettempdir(), "dark-army-avatars")
            os.makedirs(tmp, exist_ok=True)
            path = os.path.join(tmp, f"{character}-{os.path.basename(source)}")
            shutil.copyfile(source, path)
            result = self._ns["UNNotificationAttachment"] \
                .attachmentWithIdentifier_URL_options_error_(
                    "avatar", NSURL.fileURLWithPath_(path), None, None)
            attachment, error = _unpack_result(result)
            if attachment is not None:
                content.setAttachments_([attachment])
            else:
                logger.debug("Avatar attachment refused: %s", error)
        except Exception:
            logger.debug("Could not attach the avatar", exc_info=True)

    def post_notice(self, ident: str, title: str, body: str) -> bool:
        """Post one banner from Dark Army about itself. Inert by construction.

        `post`'s sibling rather than a synthetic session id through it: there is
        no category, so it carries no buttons; no thread identifier, so it
        groups with nothing; and an empty `userInfo`, so a tap reaches
        `_handle_response` with `session == ""` and returns before
        `_on_action` — no bogus id can ever be routed at a session that does
        not exist. `ident` is the request identifier, so a repost replaces
        rather than stacks.
        """
        if not self.available or self._authorized is False:
            return False
        try:
            content = self._ns["UNMutableNotificationContent"].alloc().init()
            content.setTitle_(str(title))
            content.setBody_(str(body))
            content.setUserInfo_({})
            content.setSound_(self._ns["UNNotificationSound"].defaultSound())
            request = self._ns["UNNotificationRequest"] \
                .requestWithIdentifier_content_trigger_(str(ident), content, None)
            self._center.addNotificationRequest_withCompletionHandler_(
                request, self._added_cb)
            logger.info("Notice: %r / %r", title, body)
            return True
        except Exception:
            logger.debug("Could not post a notice", exc_info=True)
            return False

    def _added_cb(self, error) -> None:
        if error is not None:
            logger.debug("Notification rejected: %s", error)

    # ── taps ─────────────────────────────────────────────────────────────────
    def _handle_response(self, action: str, session_id: str) -> None:
        """Called from the delegate. Runs on the main thread."""
        if not session_id or self._on_action is None:
            return
        try:
            self._on_action(action, session_id)
        except Exception:
            logger.warning("Notification action %r failed", action, exc_info=True)


def _make_delegate(handler: Callable[[str, str], None]):
    """Build the UNUserNotificationCenterDelegate.

    Defined in a function rather than at module scope so that importing this
    module on a machine without PyObjC — or without the framework — cannot fail:
    the class is only ever created once we know both are there.

    Both methods take a completion handler block. PyObjC cannot infer the
    signature of a method *we* define, so each declares it explicitly:
    `v@:@@@?` — returns void, takes self, _cmd, the centre, the payload, and a
    block.
    """
    import objc
    from Foundation import NSObject

    DEFAULT_ACTION = "com.apple.UNNotificationDefaultActionIdentifier"
    DISMISS_ACTION = "com.apple.UNNotificationDismissActionIdentifier"

    class BobNotificationDelegate(NSObject):
        def userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_(
                self, center, response, completion):
            try:
                action = str(response.actionIdentifier())
                info = response.notification().request().content().userInfo()
                session = str(info.get("session_id") or "")
                if action == DEFAULT_ACTION:
                    # Clicking the body means "take me there", and *there* is
                    # Dark Army's panel on that agent — see ACTION_OPEN. The Reveal
                    # button still goes to the editor for anyone who wants the
                    # terminal itself.
                    handler(ACTION_OPEN, session)
                elif action == DISMISS_ACTION:
                    # Swiping the banner away is not an instruction about the
                    # session, only about the banner. Deliberately nothing.
                    pass
                else:
                    handler(action, session)
            except Exception:
                logger.warning("Notification response failed", exc_info=True)
            _call_block(completion)

        userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_ = \
            objc.selector(
                userNotificationCenter_didReceiveNotificationResponse_withCompletionHandler_,
                signature=b"v@:@@@?")

        def userNotificationCenter_willPresentNotification_withCompletionHandler_(
                self, center, notification, completion):
            # Our own panel takes key when it opens, so we can be frontmost when
            # an alert lands. Without this the banner is swallowed exactly then.
            _call_block(completion, _PRESENT_BANNER | _PRESENT_SOUND)

        userNotificationCenter_willPresentNotification_withCompletionHandler_ = \
            objc.selector(
                userNotificationCenter_willPresentNotification_withCompletionHandler_,
                signature=b"v@:@@@?")

    return BobNotificationDelegate.alloc().init()


def _call_block(block, *args) -> None:
    """Call a completion handler we were handed, if we can.

    Dynamically loaded frameworks carry no block metadata, so this may not be
    callable at all. Failing to call it costs a system log line about a slow
    delegate; raising here would lose the tap that got us called.
    """
    if block is None:
        return
    try:
        block(*args)
    except Exception:
        logger.debug("Could not call a notification completion handler",
                     exc_info=True)


__all__ = ["Notifier", "bundle_identity", "CATEGORY_ID",
           "ACTION_OPEN", "ACTION_REVEAL", "ACTION_DISMISS", "ACTION_MUTE"]
