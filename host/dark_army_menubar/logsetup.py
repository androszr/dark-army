"""Where the app logs, and how long that file is allowed to remember.

The log used to be one file appended forever — a `logging.FileHandler` on
`~/Library/Logs/DarkArmy/dark-army.log`, opened at launch and never
rolled. Three weeks of running put 6.5 MB and 51k lines behind the panel's
"Open Log" button: every day since install in one buffer, with the line you
opened it for somewhere in the middle. A log you have to search by date is not
a log you can open from a menu.

So the live file holds **today** and nothing else. At midnight — and at the
first write after a launch that crossed one — the day is closed, gzipped beside
it as `dark-army.log.YYYY-MM-DD.gz`, and the live file starts empty;
archives older than `RETENTION_DAYS` are deleted.

Three things the stdlib does not do on its own:

- **The archive is compressed.** `TimedRotatingFileHandler` renames; the
  `rotator` / `namer` hooks turn that rename into a gzip, which is the
  difference between a directory growing ~300 KB a day and ~30 KB.
- **The archive keeps the mtime of the day it covers**, not the moment it was
  compressed — so the file's timestamp still answers "when is this from".
- **Pruning is ours, by mtime, not `backupCount`.** `backupCount` deletes by
  parsing the date back out of the filename, and only out of the names the
  handler itself wrote — it would silently skip anything rolled aside by
  `roll_stale_log()` below, which is the one file that most needs collecting.

And one thing the stdlib does at the wrong moment: the handler dates its next
rollover from the *existing file's mtime*, so an install that has been
appending since July is merely due at tonight's midnight — the 6.5 MB would
stay in front of the button for one more day. `roll_stale_log()` closes it
eagerly at startup whenever its last write landed on an earlier day than this
one, which is also what covers a machine that was asleep over midnight.

Nothing here may raise into `main()`: a failure to rotate must cost the app its
tidy log directory, never its launch. Every step is guarded, and the fallback
for a failed compression is the plain rename the handler would have done.
"""

from __future__ import annotations

import gzip
import logging
import logging.handlers
import os
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Optional

# How many days of closed archives to keep beside the live file. A week is the
# span of "it started doing this a few days ago"; past that the transcript of a
# menu-bar app is not evidence of anything.
RETENTION_DAYS = 7

LOG_NAME = "dark-army.log"
LOG_DIR_NAME = "DarkArmy"

logger = logging.getLogger("dark-army.logsetup")


def log_file_path() -> Path:
    """Where the app logs *now*. Shared by the log-file handler and the menu
    item that opens it, so the two can't drift apart."""
    return Path.home() / "Library" / "Logs" / LOG_DIR_NAME / LOG_NAME


def archive_paths() -> list[Path]:
    """Every closed day beside the live file, newest first."""
    live = log_file_path()
    found = [p for p in live.parent.glob(LOG_NAME + ".*") if p != live and p.is_file()]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def _unique(path: Path) -> Path:
    """A free name near `path` — two rolls covering the same day are rare (a
    startup roll plus that night's midnight) but must not overwrite each other.
    The counter goes *before* `.gz` so the result still opens as an archive;
    pruning is by mtime, so the shape of the suffix is otherwise free."""
    if not path.exists():
        return path
    base, ext = (path.name[:-3], ".gz") if path.name.endswith(".gz") else (path.name, "")
    n = 1
    while True:
        candidate = path.with_name(f"{base}-{n}{ext}")
        if not candidate.exists():
            return candidate
        n += 1


def compress(source: Path, dest: Path) -> Path:
    """Gzip `source` to `dest` and delete the original, preserving the mtime of
    the day it covers. Falls back to a plain rename if compression fails —
    losing the rotation is worse than losing the saved bytes."""
    dest = _unique(dest)
    stamps = None
    try:
        st = source.stat()
        stamps = (st.st_atime, st.st_mtime)
    except OSError:
        pass
    try:
        with open(source, "rb") as raw, gzip.open(dest, "wb") as gz:
            shutil.copyfileobj(raw, gz)
        os.remove(source)
    except Exception:
        logger.warning("Could not compress %s, keeping it whole", source.name, exc_info=True)
        try:
            dest.unlink()
        except OSError:
            pass
        plain = _unique(dest.with_name(dest.name[:-3]))
        os.replace(source, plain)
        dest = plain
    if stamps is not None:
        try:
            os.utime(dest, stamps)
        except OSError:
            pass
    return dest


def prune_archives(now: Optional[datetime] = None) -> list[Path]:
    """Delete archives whose day is more than `RETENTION_DAYS` behind `now`.

    By mtime rather than by name: `compress()` carries the day's own mtime onto
    the archive, so the timestamp is the day, and this then holds for every
    name we produce — including the startup roll (`roll_stale_log()`), which
    `backupCount` would not have recognised."""
    cutoff = (now or datetime.now()).timestamp() - RETENTION_DAYS * 86400
    removed = []
    for path in archive_paths():
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            removed.append(path)
        except OSError:
            logger.debug("Could not prune %s", path, exc_info=True)
    return removed


def _first_record_day(path: Path) -> Optional[date]:
    """The date of the file's first line, which `main()`'s format puts in its
    first ten characters. Unparseable (a truncated file, a format change) reads
    as unknown rather than as an error."""
    try:
        with open(path, "r", errors="replace") as fh:
            head = fh.readline(64)
    except OSError:
        return None
    try:
        return datetime.strptime(head[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def roll_stale_log(now: Optional[datetime] = None) -> Optional[Path]:
    """Close the live file at startup unless everything in it is from today.

    Two ways it isn't, and both matter. Its last write may be from an earlier
    day — the handler would get there at tonight's midnight, which is one more
    day of yesterday's lines in front of the button. Or it may have been
    *written to today and still start in July*: that is every install upgrading
    from the unrotated build, and the mtime alone calls it fresh. So the first
    record is read too, and an archive that covers more than one day says so in
    its name."""
    path = log_file_path()
    try:
        st = path.stat()
    except OSError:
        return None
    if st.st_size == 0:
        return None
    last = datetime.fromtimestamp(st.st_mtime).date()
    first = _first_record_day(path) or last
    today = (now or datetime.now()).date()
    if first >= today and last >= today:
        return None
    label = f"{first:%Y-%m-%d}" if first == last else f"{first:%Y-%m-%d}--{last:%Y-%m-%d}"
    return compress(path, path.with_name(f"{path.name}.{label}.gz"))


def _rotate(source: str, dest: str) -> None:
    """The handler's midnight rename, as a compression — then collect the old
    days, since a rollover is the one moment we know the set has changed."""
    compress(Path(source), Path(dest))
    prune_archives()


_startup_report: list[str] = []


def report_startup(log: logging.Logger) -> None:
    """Say what the tidy-up did, once there is somewhere to say it.

    `build_handler()` runs *inside* `basicConfig(handlers=[...])` — before the
    root logger has any handler at all — so an INFO record it emits is dropped
    on the floor. Its findings are queued here and drained by `main()` on the
    line after."""
    while _startup_report:
        log.info(_startup_report.pop(0))


def build_handler() -> logging.Handler:
    """The app's log-file handler: today's file, the days before it gzipped
    beside it, and nothing older than `RETENTION_DAYS` at all.

    Guarded end to end. If the tidying raises, the app still gets a handler and
    still logs; that is the whole contract this function owes `main()`."""
    path = log_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        rolled = roll_stale_log()
        if rolled is not None:
            _startup_report.append(f"Archived the previous log as {rolled.name}")
        pruned = prune_archives()
        if pruned:
            _startup_report.append(
                f"Dropped {len(pruned)} log archive(s) older than {RETENTION_DAYS} days"
            )
    except Exception:  # pragma: no cover - defensive; never block startup
        logger.warning("Could not tidy the log directory", exc_info=True)

    handler = logging.handlers.TimedRotatingFileHandler(
        path, when="midnight", backupCount=0, encoding="utf-8",
    )
    # `namer` decides what the closed day is called, `rotator` how it gets
    # there; together they replace the rename with a gzip.
    handler.namer = lambda default: default + ".gz"
    handler.rotator = _rotate
    return handler
