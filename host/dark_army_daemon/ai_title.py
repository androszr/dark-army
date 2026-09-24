"""Derive a human-friendly Claude session name from its transcript.

Claude Code writes a per-session JSONL transcript containing an
``{"type": "ai-title", "aiTitle": "...", "sessionId": "..."}`` entry — the same
short title it shows in the CLI tab header. We surface that on the "waiting"
card so it's clear WHICH session is asking. If the title isn't there yet
(brand-new session), we fall back to the first user prompt, then a short id.
"""
from __future__ import annotations

import re
import unicodedata
from collections import OrderedDict
from pathlib import Path

from .transcript_scan import iter_records


def ascii_fold(s: str) -> str:
    """Transliterate to ASCII.

    Inherited from the pixel display, whose Montserrat build had no Polish
    glyphs (ż ą ę ś ć ł ó …) and rendered raw diacritics as tofu. That display is
    gone and the surviving surfaces set real system fonts, so this is now only
    about menu-row width and predictable sorting — worth revisiting rather than
    assuming. Polish stays readable either way ("brakujące" → "brakujace").
    """
    if not s:
        return s
    # NFKD leaves ł/Ł and … untouched — handle those explicitly first.
    s = (s.replace("ł", "l").replace("Ł", "L")
          .replace("…", "...").replace("—", "-").replace("–", "-"))
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.encode("ascii", "ignore").decode("ascii")


def _first_text(content) -> str:
    """Extract plain text from a hook/transcript message content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                return item.get("text", "")
    return ""


# Titles by (path, session id) -> (mtime, title). The snapshot layer asks for
# every session's title on a 10s timer, and answering meant streaming the whole
# transcript — tens of MB for a long session — even when the file had not
# changed since the last ask. Bounded LRU: one entry per transcript ever seen
# would otherwise accumulate for the life of the daemon.
_TITLE_CACHE: "OrderedDict[tuple[str, str], tuple[float, str]]" = OrderedDict()
_TITLE_CACHE_MAX = 512


def read_ai_title(transcript_path: str, session_id: str = "") -> str:
    """Latest ``aiTitle`` for the session in the transcript, or "" if none.

    Memoised by the transcript's mtime, so an unchanged file costs one stat."""
    if not transcript_path:
        return ""
    p = Path(transcript_path)
    if not p.is_file():
        return ""

    key = (transcript_path, session_id)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return ""
    hit = _TITLE_CACHE.get(key)
    if hit is not None and hit[0] == mtime:
        _TITLE_CACHE.move_to_end(key)
        return hit[1]

    title = ""
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for obj in iter_records(f, '"ai-title"', "ai-title"):
                sid = obj.get("sessionId")
                if session_id and sid and sid != session_id:
                    continue
                t = obj.get("aiTitle")
                if t:
                    title = t  # keep the last (most recent) one
    except OSError:
        return ""

    _TITLE_CACHE[key] = (mtime, title)
    _TITLE_CACHE.move_to_end(key)
    while len(_TITLE_CACHE) > _TITLE_CACHE_MAX:
        _TITLE_CACHE.popitem(last=False)
    return title


# Synthetic "user" turns Claude Code injects into the transcript. They are not
# something the human typed, so they must never become a session's display name —
# a resumed or slash-command session otherwise shows up as
# "<local-command-caveat>Caveat: The messag…".
# Enumerated by scanning every transcript under ~/.claude/projects/ for user
# turns opening with a tag, rather than guessed.
_SYNTHETIC_PROMPT_PREFIXES = (
    "<task-notification>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<system-reminder>",
    "<user-prompt-submit-hook>",
)


# A ceiling on the *payload*, not on the row. See `_first_user_prompt`: the
# display fold belongs to each surface, but this string is held for every live
# session and re-sent on every snapshot, and an opening prompt is sometimes a
# pasted stack trace.
MAX_PROMPT_CHARS = 2000


# A reference pasted at the head of a prompt — a dragged-in file, an @-mention,
# a URL. Dropping the drag-and-drop of a screenshot is the whole point: a prompt
# that opens with `'/Users/me/Desktop/Zrzut ekranu 2026-08-16 o 15.50.40.png'`
# spends every one of the forty characters a name gets on a path, and the tab
# reads `Gav · '/Users/you/Desktop/Zrzut ekranu 20…` — a name that is the
# same for every screenshot and says nothing about any of them.
#
# Only *leading* references, and only unambiguous ones: quoted, absolute, `~`,
# `@`, or a scheme. A bare relative path is deliberately not matched — `./build.sh
# fails on main` is a sentence, and eating its subject to save eleven characters
# is the worse trade.
_LEADING_REF = re.compile(
    r"""^\s*(?:
        (['"])[~/][^'"]*\1        # a quoted absolute path — the drag-and-drop shape
      | (?:file|https?)://\S+     # a URL
      | [~/][^\s]*/[^\s]*         # a bare absolute path (needs a separator)
      | @[^\s]+                   # an @-mention of a file
    )\s*""",
    re.VERBOSE,
)


def _strip_leading_refs(text: str) -> str:
    """Whatever the person actually wrote, after the things they attached.

    Repeated because several files are often dropped in together, and the useful
    half of the prompt is whatever follows the last of them."""
    while True:
        stripped = _LEADING_REF.sub("", text, count=1)
        if stripped == text:
            return text.strip()
        text = stripped


def _first_user_prompt(transcript_path: str, limit: int = MAX_PROMPT_CHARS) -> str:
    """The instruction the person opened with, as the session's name.

    **The display cut does not live here.** It was 40 (a menu row's width),
    then 200, and both were the same mistake in different sizes: the fold
    happened in the daemon with the `…` baked into the string every surface
    shared, so a panel row that had just been given a chevron unfolded to
    exactly the same truncated sentence it was already showing. A prompt that
    ends "…przygotuj plan wprowadzenia poprawe…" mid-word is unrecoverable
    downstream, because the rest was never sent.

    So the string ships whole and each surface folds it for its own geometry:
    the panel wraps to three lines collapsed and all of them expanded, the
    terminal tab has its own `MAX_TITLE` (72), and the filter now matches words
    that used to fall off the end.

    `MAX_PROMPT_CHARS` remains, but as a **payload guard, not a display
    decision**: an opening prompt can be a pasted log, and this string rides in
    every `/api/state` for the session's life. At that size the `…` is honest —
    the brief really is longer than anything a row will ever show.
    """
    p = Path(transcript_path)
    if not p.is_file():
        return ""
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for obj in iter_records(f, '"user"', "user"):
                # isMeta marks Claude Code's own injected turns; the prefix check
                # is a belt-and-braces for transcripts that omit the flag.
                if obj.get("isMeta"):
                    continue
                text = _first_text(obj.get("message", {}).get("content"))
                text = " ".join(text.split())  # collapse whitespace
                # "(no content)" is what Claude Code writes for an empty
                # attachment turn — a name of nothing, same as a prompt that
                # was only a file.
                if (not text or text == "(no content)"
                        or text.startswith(_SYNTHETIC_PROMPT_PREFIXES)):
                    continue
                # A prompt that was *only* an attachment names nothing, so keep
                # reading rather than settling for it: the instruction the person
                # meant is in one of the turns after it.
                text = _strip_leading_refs(text)
                if not text:
                    continue
                return text[:limit] + ("…" if len(text) > limit else "")
    except OSError:
        return ""
    return ""


#: Public name for the fallback above — the daemon needs the same string both to
#: *show* (as a name of last resort) and to *send* (as the thing a generated
#: title is generated from). One reader for both, or the row and the title it is
#: about would be derived from different halves of the transcript.
first_user_prompt = _first_user_prompt


def history_title(transcript_path: str = "", session_id: str = "",
                  generated: str = "") -> str:
    """Name a history row: ai-title → Dark Army's generated title → first prompt.

    Same authority order as :func:`session_display_name`, two differences that
    are the point of a second function:

    * **No ASCII fold.** History is drawn in a system font; stripping Polish
      diacritics was a pixel-display constraint and made the row harder to
      read, not shorter in any way that mattered.
    * **No short-id fallback.** The panel already draws "—" for an empty
      title. An 8-char hex is not a name, and writing one would stop a later
      fill from finding the row still untitled.
    """
    generated = (generated or "").strip()
    if transcript_path:
        title = read_ai_title(transcript_path, session_id)
        if title and title.strip():
            return title.strip()
    if generated:
        return generated
    if transcript_path:
        prompt = _first_user_prompt(transcript_path)
        if prompt:
            return prompt.strip()
    return ""


def session_display_name(transcript_path: str, session_id: str = "",
                         generated: str = "") -> str:
    """Best human-friendly name: ai-title → Dark Army's own → first prompt → short id.

    `generated` is a title Dark Army minted itself (`session_title.py`), and it sits
    deliberately *below* the transcript's own `ai-title` and above the opening
    prompt. Below, because when Claude Code writes a title it is the better one
    and it is free — a machine where the `terminal_title` preference is off gets
    them again, and must not be stuck with whatever Dark Army generated on the day it
    was on. Above, because the prompt fallback was never a name: it is the
    request, shown because there was nothing else, which is the whole reason
    this argument exists.
    """
    title = read_ai_title(transcript_path, session_id)
    if title:
        return ascii_fold(title)
    if generated:
        return ascii_fold(generated)
    prompt = _first_user_prompt(transcript_path)
    if prompt:
        return ascii_fold(prompt)
    return session_id[:8] if session_id else ""
