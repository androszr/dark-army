# host/dark_army_menubar/pack_render.py
"""Turn the vendored agent pack into one project's files, in memory.

Pure: reads the vendored tree, returns ``dict[str, bytes]`` keyed by
project-relative path. Opens no file for writing, spawns nothing, and
imports neither a writer nor a process helper. Unresolved ``{{PLACEHOLDER}}``
tokens raise ``PackRenderError``.

Named after the upstream installer so a future re-port is a diff:
``load_profile``, ``read_fragment``, ``joined_fragment``,
``build_substitutions``, ``write_questions``, ``substitute``,
``mirror_skills``, ``shims``. ``pin_model`` / ``pin_models`` / ``role_of`` /
``shipped_roles`` are Dark Army's own: the per-role model table from the
Agent models setting lands in each brief and its two shims as they render.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from dark_army_menubar import pack_gitignore, review_steps

#: The three built-in choices. A profile is otherwise any folder holding a
#: `profile.json` — under the vendored `profiles/` or under the person's own
#: `~/.dark-army/profiles/` (`available_profiles`); `both` is web + iOS.
PROFILES = ("web", "ios", "both")
PROFILE_ALIASES = {
    "web": "web-next-vercel",
    "ios": "ios-swift-testflight",
}
PROFILE_LABELS = {"web": "Web", "ios": "iOS", "both": "Both"}
#: A profile id: the folder name. Lowercase, so it is safe in a path and on
#: the ledger, and never ``..`` or a separator.
PROFILE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
#: What a `profile.json` that leaves the Reviewers table out gets: the two
#: generic checkers the template ships, on the paths that usually carry a
#: door or an install step. A profile's own `reviewers` replaces this.
DEFAULT_REVIEWERS = (
    ("{{P}}-security-reviewer",
     "auth|login|session|token|secret|password|credential|\\.env|/api/|webhook"),
    ("{{P}}-integration-reviewer",
     "package\\.json|requirements.*\\.txt|pyproject\\.toml|Cargo\\.toml|go\\.mod|"
     "Dockerfile|Makefile|\\.github/workflows/|migrations?/|schema"),
)
LEADS_DIR = ".claude/leads/"
#: The starter ignore list: one beside `template/` (a `.gitignore` inside
#: `template/` would be rendered as a pack file and would act on this
#: repository's own template tree) and one optional fragment per profile
#: folder, which `_overlay_profile` never reads.
GITIGNORE_FILE = "gitignore.txt"
#: The area whose brief every profile ships: the fixer takes whatever falls
#: outside the areas a profile picked.
ALWAYS_AREA = "universal"
#: sha256 of every `.claude/leads/*.md` an earlier pack wrote, before the
#: briefs became project-neutral (22 Sep 2026). A lead on disk with one of
#: these digests is the pack's own, untouched, so a resync that no longer
#: ships that area may remove it; any other bytes are the person's.
LEGACY_LEAD_DIGESTS = frozenset({
    "065a38dc2c6ee397ab849b40828ad939028665db80ee54a3607454ce03d40539",
    "0748b684157a7470a4a704398c06cca7c425c7539ca2f05760f3be036c37cc4a",
    "086d9e31a53c3a02d641a96bfdf8e6ea5ed8a0f9af35e35ff379de70edc9e6b9",
    "1d483ed2c998f4233f95517b1a844c9774d52b72f416c5a3e93f78e2418075a3",
    "36b621bf8e4b95e52cd8311a46e839cd9293c1c70b66916627549daebfc082b6",
    "38bdc90d6cd806ffcd01dbbec2124c87cc870fab7b664271b1dacdb3bbe66342",
    "4f5c41d594d1106803979e5179c7585271dde67a9c5ccd2ebcf0582f7163135a",
    "783baaeeeb52e689a935f37b068747a3eb850791cba69510eb017f3d02e8c1b4",
    "79bf32cba9d4e88f75a6ed17222e1ec03fda838ac38896094558e9ae60b291b5",
    "7f7056358cca8d19f4335eeeadb27980d5015d1ba46607b4a381719d6dbbc8e2",
    "a781db957f678a269a5a4d7f568f58b70fdd06062e90fbf9458efc373a68b4e0",
    "ad9fe0e30144d106b4218ee3a8c83f7220319f639604247c94de732aea30ec7b",
    "af15e194dc27d01882339b07f164fc48b47420de41568d41735190f32e60610f",
    "bcf0802289cbf529185acd0827601ec5d71a12d82da10b27a7b2a08d38a0cc60",
    "cc823688242bcbd6ca64bbc29629401a94839c8c07d1e1f146cce7ea5f0f76a8",
    "f1a0158a3c417fc51c476771c72a133665eed8085ac44a196af3d943d2616808",
    # pocket.md once the cast name Ptyś became the ASCII Ptys (25 Sep 2026).
    "8e0461ef90fc7acd593da1fdeffce2e0fc9b296ae976d2e8577d7995c494abac",
})
PREFIX_RE = re.compile(r"^[a-z][a-z0-9]{0,7}$")
_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")
_FRONTMATTER = re.compile(r"---\n(.*?)\n---\n", re.S)
_WRITE_TOOLS = {"Write", "Edit", "NotebookEdit", "MultiEdit"}
SKIP_SKILL_PREFIX = "gitnexus-"
OPENAI_YAML = "agents/openai.yaml"

BEGIN_MARK = (
    "<!-- BEGIN DARK ARMY PACK — managed, edits here are overwritten -->"
)
END_MARK = "<!-- END DARK ARMY PACK -->"
SPLICED_KEYS = frozenset({
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    "docs/context.md",
    ".claude/review.md",
})
#: Spliced files the project is meant to *fill in*, not merely sit beside.
#: `docs/context.md` is a template of blanks — the Identity table's real
#: gates, the architecture, the conventions — so its whole body lives between
#: the markers, and a resync that re-spliced it would put the blanks back over
#: the project's answers (vir-sunset lost its gate rows that way, 24 Sep 2026).
#: `pack_install` rewrites such a region only while it is still the bytes the
#: pack last wrote (`managed_digest`, recorded per project in the ledger's
#: `pack_digests`); once the project has edited it, the file is the
#: project's. A file with no markers at all is the project's own text: the
#: pack splices its region in above it once, and never rewrites that text.
PROJECT_FILLED_KEYS = frozenset({
    "docs/context.md",
})
#: Whole files the pack seeds but a project routinely adapts to itself: the
#: guard scripts (which Debug entitlements file, which privacy keys this app
#: can reach) and the CI workflows. The same rule as `PROJECT_FILLED_KEYS`,
#: applied to the whole file because these carry no markers: the pack
#: rewrites one only while it is still the bytes the pack last wrote there
#: (`file_digest`, in the ledger's `pack_digests`). Once the project has
#: edited it, it is the project's — and a file that differs from the pack
#: with no digest on record (a ledger row from before this rule) is treated
#: as edited, never overwritten. arpg-web lost its adapted
#: `check-entitlements.py` and `check-privacy-strings.py` to a resync that
#: way (24 Sep 2026) and its gate went red on files nobody in it had touched.
#: The cost: a later fix to a shipped script does not reach a project that
#: edited its copy; deleting the file lets the next resync seed it again.
PROJECT_ADAPTED_PREFIXES = (
    "scripts/",
    ".github/workflows/",
)


def project_adapted(key: str) -> bool:
    """Whether ``key`` is a whole file the project may take over by editing."""
    return key.startswith(PROJECT_ADAPTED_PREFIXES)
SETTINGS_KEY = ".claude/settings.json"

QUESTIONS_HEADER = """# Interview questions

Ask **at most 3**, one at a time. Try to answer each from the codebase first —
a question the tree already answers is a wasted turn. `docs/context.md`
settles most architecture branches outright.

Pick the branches that actually apply to the idea. Skip the rest.

---
"""


class PackRenderError(Exception):
    """The pack could not be rendered. The message is safe to show."""


def vendor_dir() -> Path:
    """The vendored tree next to this module (checkout and package data)."""
    return Path(__file__).resolve().parent / "agent_pack"


def default_prefix(project: str) -> str:
    """A 1-8 char agent prefix derived from the project folder name."""
    parts = re.findall(r"[A-Za-z0-9]+", project or "")
    if len(parts) >= 2:
        initials = "".join(part[0] for part in parts).lower()
        if PREFIX_RE.fullmatch(initials[:8]):
            return initials[:8]
    slug = re.sub(r"[^a-z0-9]", "", (project or "").lower())
    if not slug:
        slug = "app"
    if slug[0].isdigit():
        slug = "p" + slug
    slug = slug[:8]
    if not PREFIX_RE.fullmatch(slug):
        slug = "app"
    return slug


def valid_profile_id(name: str) -> bool:
    """Whether ``name`` could name a profile at all. No file is touched —
    the AppKit thread's cheap check before a press reaches a worker."""
    name = str(name or "")
    return name == "both" or bool(
        PROFILE_ID_RE.fullmatch(PROFILE_ALIASES.get(name, name)))


def _profile_dir(name: str, source: Path,
                 user_dir: Path | None = None) -> tuple[Path, str] | None:
    """The folder a profile id names and where it came from, or None.

    The vendored tree wins: a folder of the person's own with a shipped
    name is never read. A folder of their own that is a symlink is refused
    — the pack copies what it holds into a project, and a link could point
    it anywhere."""
    full = PROFILE_ALIASES.get(name, name)
    if not PROFILE_ID_RE.fullmatch(full):
        return None
    shipped = Path(source) / "profiles" / full
    if (shipped / "profile.json").is_file():
        return shipped, "shipped"
    if user_dir is None or name in PROFILE_ALIASES:
        return None
    mine = Path(user_dir) / full
    if mine.is_symlink() or not mine.is_dir():
        return None
    manifest = mine / "profile.json"
    if manifest.is_symlink() or not manifest.is_file():
        return None
    return mine, "yours"


def _pairs(data: dict, key: str, name: str) -> list[list[str]]:
    raw = data.get(key, [])
    ok = isinstance(raw, list) and all(
        isinstance(row, list) and len(row) == 2
        and all(isinstance(cell, str) and cell.strip() for cell in row)
        for row in raw)
    if not ok:
        raise PackRenderError(
            f"profile {name!r}: {key} must be a list of [name, value] pairs")
    return [list(row) for row in raw]


def _strings(data: dict, key: str, name: str) -> list[str]:
    raw = data.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise PackRenderError(f"profile {name!r}: {key} must be a list of strings")
    return list(raw)


def normalise_profile(data, name: str) -> dict:
    """A `profile.json` made whole: every key the renderer reads, with a
    default where the file leaves it out, so one short file is a profile.

    Raises `PackRenderError` (a message safe to show) on a wrong shape.
    ``areas`` stays ``None`` when absent: every area's brief ships."""
    if not isinstance(data, dict):
        raise PackRenderError(f"profile {name!r} is not a JSON object")
    out = dict(data)
    out["name"] = str(data.get("name") or name)
    out["label"] = str(data.get("label") or "")
    out["summary"] = str(data.get("summary") or "")
    out["src_dir"] = str(data.get("src_dir") or "")
    out["gates"] = _pairs(data, "gates", name)
    out["reviewers"] = (_pairs(data, "reviewers", name) if "reviewers" in data
                        else [list(row) for row in DEFAULT_REVIEWERS])
    first = out["reviewers"][0][0] if out["reviewers"] else ""
    out["primary_reviewer"] = str(
        data.get("primary_reviewer") or first or "{{P}}-security-reviewer")
    out["skill_rows"] = _strings(data, "skill_rows", name)
    out["settings_allow"] = _strings(data, "settings_allow", name)
    out["one_time_steps"] = _strings(data, "one_time_steps", name)
    try:
        out["after_steps"] = review_steps.check_steps(
            data.get("after_steps"), name)
    except ValueError as exc:
        raise PackRenderError(str(exc)) from exc
    areas = data.get("areas")
    if areas is not None:
        areas = _strings(data, "areas", name)
    out["areas"] = areas
    return out


def load_profile(name: str, source: Path, user_dir: Path | None = None) -> dict:
    found = _profile_dir(name, source, user_dir)
    if found is None:
        raise PackRenderError(f"unknown profile {name!r}")
    folder, origin = found
    try:
        data = json.loads((folder / "profile.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackRenderError(
            f"profile {name!r}: profile.json could not be read") from exc
    data = normalise_profile(data, PROFILE_ALIASES.get(name, name))
    data["_dir"] = folder
    data["_origin"] = origin
    return data


_LISTED: dict = {}


def available_profiles(source: Path | None = None,
                       user_dir: Path | None = None) -> list[dict]:
    """Every profile a person can press, in menu order: the three built-ins,
    any other vendored folder, then the person's own folders under
    ``user_dir``. Each row is ``{id, label, summary, origin}``; a folder
    whose `profile.json` does not read or has the wrong shape is left out.

    Reads only, and remembers the answer until a `profile.json` or the
    folder listing changes, so a panel push costs a few ``stat`` calls."""
    root = Path(source) if source is not None else vendor_dir()
    candidates: list[tuple[str, Path, str]] = []
    shipped_names = set(PROFILE_ALIASES.values())
    for folder in sorted((root / "profiles").glob("*/profile.json")):
        name = folder.parent.name
        if name not in shipped_names and PROFILE_ID_RE.fullmatch(name):
            candidates.append((name, folder, "shipped"))
    taken = set(PROFILES) | shipped_names | {c[0] for c in candidates}
    if user_dir is not None and Path(user_dir).is_dir():
        for folder in sorted(Path(user_dir).glob("*/profile.json")):
            name = folder.parent.name
            if name in taken or not PROFILE_ID_RE.fullmatch(name):
                continue
            if _profile_dir(name, root, user_dir) is None:
                continue
            candidates.append((name, folder, "yours"))
    stamps = []
    for name, manifest, _origin in candidates:
        try:
            stamps.append((name, manifest.stat().st_mtime_ns))
        except OSError:
            stamps.append((name, 0))
    for builtin in ("web", "ios"):
        try:
            stamps.append((builtin, (root / "profiles" / PROFILE_ALIASES[builtin]
                                     / "profile.json").stat().st_mtime_ns))
        except OSError:
            stamps.append((builtin, 0))
    key = (str(root), str(user_dir), tuple(stamps))
    if _LISTED.get("key") == key:
        return [dict(row) for row in _LISTED["rows"]]
    rows: list[dict] = []
    summaries = {}
    for builtin in ("web", "ios"):
        try:
            summaries[builtin] = load_profile(builtin, root)["summary"]
        except PackRenderError:
            summaries[builtin] = ""
    for builtin in PROFILES:
        summary = summaries.get(builtin) or " + ".join(
            s for s in (summaries.get("web"), summaries.get("ios")) if s)
        rows.append({"id": builtin, "label": PROFILE_LABELS[builtin],
                     "summary": summary, "origin": "shipped"})
    for name, _manifest, origin in candidates:
        try:
            loaded = load_profile(name, root, user_dir)
        except PackRenderError:
            continue
        rows.append({"id": name, "label": loaded["label"] or name,
                     "summary": loaded["summary"], "origin": origin})
    _LISTED["key"] = key
    _LISTED["rows"] = rows
    return [dict(row) for row in rows]


def read_fragment(profile: dict, name: str) -> str:
    folder = profile["_dir"] / "fragments"
    path = folder / f"{name}.md"
    if folder.is_symlink() or path.is_symlink() or not path.is_file():
        return ""  # a profile of the person's own must not reach outside it
    return path.read_text(encoding="utf-8").rstrip() + "\n"


def joined_fragment(profiles: list[dict], name: str, *, heading: bool) -> str:
    parts = []
    for profile in profiles:
        body = read_fragment(profile, name)
        if not body:
            continue
        if heading and len(profiles) > 1:
            parts.append(f"### {profile['name']}\n\n{body}")
        else:
            parts.append(body)
    return "\n".join(parts).rstrip() + "\n" if parts else ""


def build_substitutions(
    profiles: list[dict],
    prefix: str,
    project: str,
    app: str = "",
    gitnexus_repo: str = "",
) -> dict[str, str]:
    derived_app = app or re.sub(r"[^A-Za-z0-9]", "", project.title())
    gate_rows = []
    for profile in profiles:
        for key, cmd in profile["gates"]:
            gate_rows.append(f"| `{key}` | `{cmd}` |")
    reviewer_rows = []
    for profile in profiles:
        for agent, regex in profile["reviewers"]:
            reviewer_rows.append(f"| `{agent}` | `{regex}` |")
    skill_rows = [row for profile in profiles for row in profile["skill_rows"]]
    # JSON-quoted, not pasted: a profile of the person's own may carry a
    # quote or a backslash, and a bare paste would break — or add keys to —
    # the settings file. The shipped rows quote to the same bytes as before.
    allow_rows = [
        "      " + json.dumps(row, ensure_ascii=False)
        for profile in profiles
        for row in profile["settings_allow"]
    ]
    allow_block = (",\n" + ",\n".join(allow_rows)) if allow_rows else ""
    return {
        "P": prefix,
        "PROJECT": project,
        "PROJECT_UPPER": project.upper(),
        "APP": derived_app,
        "SRC_DIR": ", ".join(p["src_dir"] for p in profiles if p["src_dir"]) or ".",
        "GATE_ROWS": "\n".join(gate_rows),
        "REVIEWER_ROWS": "\n".join(reviewer_rows),
        "REVIEWER_AGENT": profiles[0]["primary_reviewer"],
        "GITNEXUS_REPO": gitnexus_repo or project,
        "SKILL_ROWS": "\n".join(skill_rows),
        "ALLOW_ROWS": allow_block,
        "CONVENTION_ROWS": joined_fragment(
            profiles, "convention-rows", heading=True),
        "BLIND_SPOT_ROWS": joined_fragment(
            profiles, "blind-spots", heading=True),
        "HUNT": joined_fragment(profiles, "hunt", heading=True),
        "CONVENTIONS": joined_fragment(
            profiles, "conventions", heading=False),
        "PLANNER_RULES": joined_fragment(
            profiles, "planner-rules", heading=True),
        "IMPLEMENTER_RULES": joined_fragment(
            profiles, "implementer-rules", heading=True),
        "RISK_DOMAINS": joined_fragment(
            profiles, "risk-domains", heading=True),
    }


def write_questions(profiles: list[dict]) -> bytes:
    body = joined_fragment(profiles, "questions", heading=True)
    return (QUESTIONS_HEADER + "\n" + body).encode("utf-8")


def _apply_placeholders(text: str, subs: dict[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda m: subs.get(m.group(1), m.group(0)), text)


def substitute(
    mapping: dict[str, bytes], subs: dict[str, str]
) -> tuple[dict[str, bytes], list[str]]:
    """Replace placeholders in names and contents. Returns leftovers found."""
    leftovers: list[str] = []
    renamed: dict[str, bytes] = {}
    for key in sorted(mapping, key=lambda k: -len(Path(k).parts)):
        new_key = key
        for _ in range(2):
            new_key = _apply_placeholders(new_key, subs)
        renamed[new_key] = mapping[key]
    out: dict[str, bytes] = {}
    for key, data in renamed.items():
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            out[key] = data
            continue
        for _ in range(2):
            text = _apply_placeholders(text, subs)
        for match in _PLACEHOLDER.finditer(text):
            leftovers.append(f"{key}: {{{{{match.group(1)}}}}}")
        for match in _PLACEHOLDER.finditer(key):
            leftovers.append(f"{key}: {{{{{match.group(1)}}}}}")
        out[key] = text.encode("utf-8")
    return out, sorted(set(leftovers))


def _frontmatter(text: str) -> dict[str, str]:
    match = _FRONTMATTER.match(text)
    if not match:
        return {}
    out: dict[str, str] = {}
    key = None
    for line in match.group(1).splitlines():
        if re.match(r"^[a-zA-Z_]+:", line):
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip().strip('"')
        elif key and line.startswith(" "):
            out[key] += " " + line.strip().strip('"')
    return out


def openai_yaml(skill_md: bytes, skill_name: str) -> bytes:
    fm = _frontmatter(skill_md.decode("utf-8"))
    name = fm.get("name", skill_name)
    desc = fm.get("description", "")
    short = re.split(r"(?<=[.!?])\s", desc, maxsplit=1)[0][:120] if desc else name
    display = " ".join(w.capitalize() for w in name.replace("-", " ").split())
    text = (
        "interface:\n"
        f'  display_name: "{display}"\n'
        f'  short_description: "{short.replace(chr(34), "")}"\n'
        f'  default_prompt: "Use ${name} for this task."\n'
    )
    return text.encode("utf-8")


def mirror_skills(mapping: dict[str, bytes]) -> dict[str, bytes]:
    """Byte-copy ``.claude/skills/*`` onto ``.agents/skills/*`` in the mapping."""
    prefix = ".claude/skills/"
    skills: dict[str, dict[str, bytes]] = {}
    for key, data in mapping.items():
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix):]
        name, _, inner = rest.partition("/")
        if not name or not inner:
            continue
        skills.setdefault(name, {})[inner] = data
    extra: dict[str, bytes] = {}
    for name, files in skills.items():
        if name.startswith(SKIP_SKILL_PREFIX):
            continue
        for inner, data in files.items():
            extra[f".agents/skills/{name}/{inner}"] = data
        skill_md = files.get("SKILL.md")
        if skill_md is not None:
            extra[f".agents/skills/{name}/{OPENAI_YAML}"] = openai_yaml(
                skill_md, name)
    mapping.update(extra)
    return mapping


def sandbox_for(fm: dict[str, str]) -> str:
    tools = {t.strip() for t in fm.get("tools", "").split(",") if t.strip()}
    return "workspace-write" if tools & _WRITE_TOOLS else "read-only"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def role_of(key: str, prefix: str = "") -> str:
    """A brief's role: its stem minus the project's ``<prefix>-``.

    ``.claude/agents/xy-bug-auditor.md`` with prefix ``xy`` is
    ``bug-auditor``; the vendored ``{{P}}-planner.md`` with no prefix is
    ``planner``. With no prefix given, whatever precedes the first ``-`` is
    taken as the prefix. A stem that does not carry the prefix is returned
    whole — a hand-named brief is its own role.
    """
    stem = Path(key).stem
    if prefix and stem.startswith(prefix + "-"):
        return stem[len(prefix) + 1:]
    if not prefix and "-" in stem:
        return stem.partition("-")[2]
    return stem


def pin_model(text: str, model: str) -> str:
    """Write ``model: <id>`` into a brief's frontmatter, or take it out.

    Inserted after the ``name:`` line (at the top when there is none),
    replacing an existing ``model:`` line rather than adding a second; an
    empty ``model`` removes any such line. Text with no frontmatter is
    returned unchanged. Pure string work — the pack writes nothing here.
    """
    match = _FRONTMATTER.match(text)
    if not match:
        return text
    lines = [line for line in match.group(1).split("\n")
             if not re.match(r"^model:", line)]
    if model:
        at = 0
        for index, line in enumerate(lines):
            if re.match(r"^name:", line):
                at = index + 1
                break
        lines.insert(at, f"model: {model}")
    body = "\n".join(lines)
    return text[:match.start(1)] + body + text[match.end(1):]


def pin_effort(text: str, level: str) -> str:
    """Write ``effort: <level>`` into a brief's frontmatter, or take it out.

    `pin_model`'s twin: placed after the ``model:`` line (after ``name:`` when
    there is none), replacing an existing ``effort:`` line rather than adding a
    second; an empty ``level`` removes any such line. Text with no frontmatter
    is returned unchanged. Pure string work."""
    match = _FRONTMATTER.match(text)
    if not match:
        return text
    lines = [line for line in match.group(1).split("\n")
             if not re.match(r"^effort:", line)]
    if level:
        at = 0
        for pattern in (r"^model:", r"^name:"):
            hit = next((i for i, line in enumerate(lines)
                        if re.match(pattern, line)), None)
            if hit is not None:
                at = hit + 1
                break
        lines.insert(at, f"effort: {level}")
    body = "\n".join(lines)
    return text[:match.start(1)] + body + text[match.end(1):]


def shipped_roles(source: Path | None = None) -> frozenset[str]:
    """Every role the vendored pack can write a brief for: the stems of
    ``template/.claude/agents/*.md`` and each profile's ``agents/*.md``,
    minus their ``{{P}}-`` prefix. Listed with ``glob``, never opened — the
    renderer stays pure. A missing tree is the empty set."""
    root = Path(source) if source is not None else vendor_dir()
    found: set[str] = set()
    for path in root.glob("template/.claude/agents/*.md"):
        found.add(role_of(path.name, "{{P}}"))
    for path in root.glob("profiles/*/agents/*.md"):
        found.add(role_of(path.name, "{{P}}"))
    return frozenset(found)


#: The reasoning effort the Codex companion files have always carried, spelled
#: here without importing `agent_models` so the renderer stays a pure string
#: module (`SHIPPED_WORKERS`' precedent). `test_agent_pack_render` pins it
#: equal to `agent_models.SHIPPED_EFFORTS["codex"]` for the roles.
SHIPPED_CODEX_EFFORT = "high"


def codex_shim(name: str, fm: dict[str, str], sandbox: str,
               model: str = "", effort: str = SHIPPED_CODEX_EFFORT) -> str:
    desc = _one_line(fm.get("description", name)).replace('"', "'")
    guard = (
        "Verify independently, cite evidence, and do not edit any file or repair failures."
        if sandbox == "read-only"
        else "Preserve the user's baseline changes, and never commit, push, deploy, or rewrite acceptance criteria."
    )
    # The role's Codex model from Dark Army's Agent models setting, after
    # `description` where Codex's custom-agent struct reads it; empty writes
    # no line, so an untouched setting renders yesterday's bytes.
    model_line = f'model = "{model}"\n' if model else ""
    # The role's reasoning effort: the shipped level by default, so an
    # untouched install renders yesterday's bytes; an empty one writes no line.
    effort_line = f'model_reasoning_effort = "{effort}"\n' if effort else ""
    return (
        "# GENERATED by pack_render.shims from .claude/agents/"
        f"{name}.md — do not edit; edit the markdown brief and regenerate.\n"
        f'name = "{name}"\n'
        f'description = "{desc}"\n'
        f"{model_line}"
        f'sandbox_mode = "{sandbox}"\n'
        f"{effort_line}"
        'developer_instructions = """\n'
        f"Read AGENTS.md, docs/context.md, and .claude/agents/{name}.md completely before working. "
        "The markdown file is the authoritative role specification; follow its inputs, method, rules and output format exactly. "
        f"{guard}\n"
        '"""\n'
    )


def grok_shim(name: str, fm: dict[str, str], sandbox: str,
              model: str = "", effort: str = "") -> str:
    desc = _one_line(fm.get("description", name)).replace('"', "'")
    mode = (
        "read-only: read, search and run commands, never edit a file"
        if sandbox == "read-only"
        else "read-write: edit files, run the gates, never commit or push"
    )
    # The role's Grok model, after `description:` in the frontmatter Grok's
    # agent-definition parser reads. Grok also discovers `.claude/agents`,
    # so this line is what keeps a Claude alias out of a Grok spawn.
    model_line = f"model: {model}\n" if model else ""
    # The role's Grok effort, `effort:` after `model:`; none writes no line.
    model_line += f"effort: {effort}\n" if effort else ""
    return (
        "---\n"
        f"name: {name}\n"
        f'description: "{desc}"\n'
        f"{model_line}"
        "---\n\n"
        "<!-- GENERATED by pack_render.shims from .claude/agents/"
        f"{name}.md — do not edit; edit the markdown brief and regenerate. -->\n\n"
        f"Read `AGENTS.md`, `docs/context.md`, and `.claude/agents/{name}.md` completely before working. "
        "The markdown brief is the authoritative role specification: follow its inputs, method, rules and output format exactly.\n\n"
        f"Capability for this role: **{mode}**. "
        "Where the brief names a tool by its Claude name (Read, Grep, Glob, Bash, Write, Edit), use the equivalent tool you have.\n"
    )


def _model_for(models, provider: str, role: str) -> str:
    """One cell of a resolved `{provider: {role: model}}` table, or ``""``."""
    if not isinstance(models, dict):
        return ""
    row = models.get(provider)
    if not isinstance(row, dict):
        return ""
    return str(row.get(role) or "")


def _effort_cell(efforts, provider: str, role: str):
    """One cell of a resolved `{provider: {role: level}}` table: the level,
    `""` for an explicit Default, `None` where the table has no such cell."""
    if not isinstance(efforts, dict):
        return None
    row = efforts.get(provider)
    if not isinstance(row, dict) or role not in row:
        return None
    return str(row.get(role) or "")


def _effort_for(efforts, provider: str, role: str) -> str:
    """The level a companion file carries. Codex falls back to
    `SHIPPED_CODEX_EFFORT` where there is no table or no cell — so
    `render(efforts=None)` is byte-identical to before — and only an explicit
    `""` removes the line. Claude and Grok have no shipped level."""
    cell = _effort_cell(efforts, provider, role)
    if cell is None:
        return SHIPPED_CODEX_EFFORT if provider == "codex" else ""
    return cell


def shims(mapping: dict[str, bytes], models=None,
          prefix: str = "", efforts=None) -> dict[str, bytes]:
    """One Codex and one Grok companion per Claude brief. ``models`` is the
    resolved per-provider table; each shim carries its own provider's value
    for the brief's role (``role_of``), and none where the value is empty."""
    folder = ".claude/agents/"
    for key, data in list(mapping.items()):
        if not key.startswith(folder) or not key.endswith(".md"):
            continue
        stem = Path(key).stem
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        fm = _frontmatter(text)
        name = fm.get("name", stem)
        sandbox = sandbox_for(fm)
        role = role_of(key, prefix)
        mapping[f".codex/agents/{name}.toml"] = codex_shim(
            name, fm, sandbox,
            model=_model_for(models, "codex", role),
            effort=_effort_for(efforts, "codex", role)).encode("utf-8")
        mapping[f".grok/agents/{name}.md"] = grok_shim(
            name, fm, sandbox,
            model=_model_for(models, "grok", role),
            effort=_effort_for(efforts, "grok", role)).encode("utf-8")
    return mapping


def pin_models(mapping: dict[str, bytes], models, prefix: str = "") -> dict[str, bytes]:
    """Write the Claude slot's value into each brief's frontmatter. With no
    table, or an empty cell, a brief is left byte-identical."""
    if not isinstance(models, dict):
        return mapping
    folder = ".claude/agents/"
    for key, data in list(mapping.items()):
        if not key.startswith(folder) or not key.endswith(".md"):
            continue
        model = _model_for(models, "claude", role_of(key, prefix))
        if not model:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        mapping[key] = pin_model(text, model).encode("utf-8")
    return mapping


def pin_efforts(mapping: dict[str, bytes], efforts,
                prefix: str = "") -> dict[str, bytes]:
    """Write the Claude slot's effort into each brief's frontmatter. With no
    table, or an empty cell, a brief is left byte-identical."""
    if not isinstance(efforts, dict):
        return mapping
    folder = ".claude/agents/"
    for key, data in list(mapping.items()):
        if not key.startswith(folder) or not key.endswith(".md"):
            continue
        level = _effort_cell(efforts, "claude", role_of(key, prefix))
        if not level:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        mapping[key] = pin_effort(text, level).encode("utf-8")
    return mapping


def pin_toml_effort(text: str, level: str) -> str:
    """A Codex shim's ``model_reasoning_effort = "<level>"`` line, written or
    taken out — the function `pin_toml_model`'s docstring reserved.

    Replaces an existing line in place; with none, written after
    ``sandbox_mode =`` (where `codex_shim` writes it), else after ``model =`` /
    ``description =``. An empty ``level`` removes the line. Pure."""
    lines = text.split("\n")
    where = next((i for i, line in enumerate(lines)
                  if re.match(r"^model_reasoning_effort\s*=", line)), None)
    new = f'model_reasoning_effort = "{level}"'
    if where is not None:
        if level:
            lines[where] = new
        else:
            del lines[where]
        return "\n".join(lines)
    if not level:
        return text
    at = 0
    for pattern in (r"^sandbox_mode\s*=", r"^model\s*=", r"^description\s*="):
        hit = next((i for i, line in enumerate(lines)
                    if re.match(pattern, line)), None)
        if hit is not None:
            at = hit + 1
            break
    lines.insert(at, new)
    return "\n".join(lines)


def pin_toml_model(text: str, model: str) -> str:
    """A Codex shim's ``model = "<id>"`` line, written or taken out.

    Placed after ``description =`` (where `codex_shim` writes it), replacing
    an existing ``model =`` line rather than adding a second — never
    ``model_reasoning_effort``. An empty ``model`` removes the line. Pure."""
    lines = [line for line in text.split("\n")
             if not re.match(r"^model\s*=", line)]
    if model:
        at = 0
        for index, line in enumerate(lines):
            if re.match(r"^description\s*=", line):
                at = index + 1
                break
        lines.insert(at, f'model = "{model}"')
    return "\n".join(lines)


#: Dark Army's own checkout is never rendered — its ``bc-*`` briefs are the
#: hand-kept source, not the vendored template — so the Agent models setting
#: reaches it by pinning the one model line in place, file by file.
#: The shunt skill's ``workers.json`` twins are not pinned here: they stay
#: byte-equal to the pack template (`tools/sync_shunt_skill.py --check`).
OWN_PREFIX = "bc"


def pin_own_models(mapping: dict[str, bytes], models,
                   prefix: str = OWN_PREFIX, efforts=None) -> dict[str, bytes]:
    """The model lines of an existing checkout's briefs and shims, pinned in
    place from a resolved ``{provider: {role: model}}`` table.

    ``mapping`` is the checkout's current bytes for ``.claude/agents/*.md``
    (the Claude cell), ``.codex/agents/*.toml`` (Codex), ``.grok/agents/*.md``
    (Grok), ``bc-*`` stems only. Unlike `pin_models`,
    an empty cell **removes** the line: Default means no flag, and a stale
    pin must not outlive the choice. Only the entries whose bytes change are
    returned; nothing but the model line is touched. Pure.

    ``efforts`` — a resolved ``{provider: {role: level}}`` table, or ``None``
    for none — pins the effort line the same way, in the same pass: an empty
    cell removes it (Codex's shipped ``high`` is a cell like any other), a role
    with no cell is left as it is, and ``None`` touches no effort line."""
    if not isinstance(models, dict):
        return {}
    changed: dict[str, bytes] = {}
    for key, data in mapping.items():
        if not key.rsplit("/", 1)[-1].startswith(prefix + "-"):
            continue  # a hand-named brief keeps whatever line it has
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        role = role_of(key, prefix)
        if key.startswith(".claude/agents/") and key.endswith(".md"):
            new = pin_model(text, _model_for(models, "claude", role))
            level = _effort_cell(efforts, "claude", role)
            if level is not None:
                new = pin_effort(new, level)
        elif key.startswith(".codex/agents/") and key.endswith(".toml"):
            new = pin_toml_model(text, _model_for(models, "codex", role))
            if isinstance(efforts, dict):
                new = pin_toml_effort(new, _effort_for(efforts, "codex", role))
        elif key.startswith(".grok/agents/") and key.endswith(".md"):
            new = pin_model(text, _model_for(models, "grok", role))
            level = _effort_cell(efforts, "grok", role)
            if level is not None:
                new = pin_effort(new, level)
        else:
            continue
        new = new.encode("utf-8")
        if new != data:
            changed[key] = new
    return changed


SHUNT_SKILL_KEY = ".claude/skills/shunt/SKILL.md"
WORKERS_KEY = ".claude/skills/shunt/workers.json"
#: The worker per provider the pack ships in `workers.json` when no table
#: says otherwise: `card_prepare.WORKER_MODELS`, spelled here without the
#: import so the renderer stays a pure string module. `test_agent_pack_render`
#: pins the two equal and the template file byte-identical to this.
SHIPPED_WORKERS = {"claude": "haiku", "codex": "gpt-6-luna", "grok": "grok-4.5"}


def ships_shunt(source: Path | None = None) -> bool:
    """Whether the vendored pack carries the shunt skill
    (``template/.claude/skills/shunt/SKILL.md``). Stat only, never opened —
    the renderer stays pure. What `agent_models.slots_for(worker=)` is told,
    so the settings window draws a Worker row only where the pack can write
    a `workers.json` for it."""
    root = Path(source) if source is not None else vendor_dir()
    return (root / "template" / SHUNT_SKILL_KEY).is_file()


def workers_json(models) -> bytes:
    """The bytes of ``workers.json`` for a resolved table: each provider's
    `worker` cell, the shipped default where the cell is empty or absent.
    One line, sorted keys — the same shape the template file carries, so a
    render with the shipped table is byte-identical to the template."""
    table = {}
    for provider, shipped in SHIPPED_WORKERS.items():
        chosen = _model_for(models, provider, "worker")
        table[provider] = chosen or shipped
    return (json.dumps(table, sort_keys=True) + "\n").encode("utf-8")


def pin_worker_models(mapping: dict[str, bytes], models) -> dict[str, bytes]:
    """Rewrite the shunt skill's ``workers.json`` from the table's `worker`
    cells. With no table the template bytes stand; with no skill in the
    mapping nothing is written. Called from ``render`` **before**
    ``mirror_skills`` so the ``.agents`` twin is byte-identical."""
    if not isinstance(models, dict) or WORKERS_KEY not in mapping:
        return mapping
    mapping[WORKERS_KEY] = workers_json(models)
    return mapping


def executable_keys(mapping: dict[str, bytes]) -> frozenset[str]:
    """Project-relative paths that must land mode 0755."""
    return frozenset(
        key for key in mapping
        if key.endswith(".sh")
        or (key.startswith("scripts/") and key.endswith(".py"))
    )


def _through_a_link(src: Path, path: Path) -> bool:
    """Whether ``path`` is, or sits under, a symlink inside ``src``."""
    here = src
    for part in path.relative_to(src).parts:
        here = here / part
        if here.is_symlink():
            return True
    return False


def _read_tree(src: Path, dest_prefix: str = "") -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    if src.is_symlink() or not src.is_dir():
        return out  # a linked folder reaches outside the profile
    for path in sorted(src.rglob("*")):
        if not path.is_file() or path.name == ".DS_Store":
            continue
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if _through_a_link(src, path):
            continue  # a profile of the person's own must not reach outside it
        rel = path.relative_to(src).as_posix()
        key = f"{dest_prefix}{rel}" if dest_prefix else rel
        out[key] = path.read_bytes()
    return out


def _overlay_profile(profile: dict, mapping: dict[str, bytes]) -> None:
    root = profile["_dir"]
    mapping.update(_read_tree(root / "agents", ".claude/agents/"))
    mapping.update(_read_tree(root / "skills", ".claude/skills/"))
    mapping.update(_read_tree(root / "workflows", ".github/workflows/"))
    mapping.update(_read_tree(root / "scripts", "scripts/"))
    mapping.update(_read_tree(root / "leads", LEADS_DIR))
    for preflight in sorted(root.glob("PREFLIGHT-*.md")):
        if preflight.is_symlink() or not preflight.is_file():
            continue
        mapping[f".claude/skills/ship/{preflight.name}"] = preflight.read_bytes()


def lead_areas(profiles: list[dict]) -> set[str] | None:
    """The area briefs a render ships: the union of each profile's
    ``areas`` plus `ALWAYS_AREA`, or ``None`` — all of them — when any
    profile names none."""
    chosen: set[str] = set()
    for profile in profiles:
        areas = profile.get("areas")
        if areas is None:
            return None
        chosen.update(str(a).strip().lower() for a in areas)
    chosen.add(ALWAYS_AREA)
    return chosen


def keep_leads(mapping: dict[str, bytes], keep: set[str] | None) -> dict[str, bytes]:
    """Drop the `.claude/leads/*.md` a profile did not pick. A card in an
    area with no brief simply gets no read instruction at Start."""
    if keep is None:
        return mapping
    for key in [k for k in mapping if k.startswith(LEADS_DIR)]:
        if Path(key).stem not in keep:
            del mapping[key]
    return mapping


def shipped_lead_digests(source: Path | None = None,
                         user_dir: Path | None = None) -> frozenset[str]:
    """sha256 of every lead brief the pack could have written: the legacy
    set, the template's, every vendored profile's and the person's own
    profiles'. The installer removes a lead only when its bytes are one of
    these — never a brief somebody wrote or edited."""
    root = Path(source) if source is not None else vendor_dir()
    found = set(LEGACY_LEAD_DIGESTS)
    folders = [root / "template" / ".claude" / "leads"]
    folders += sorted((root / "profiles").glob("*/leads"))
    if user_dir is not None and Path(user_dir).is_dir():
        folders += sorted(Path(user_dir).glob("*/leads"))
    for folder in folders:
        for data in _read_tree(folder).values():
            found.add(hashlib.sha256(data).hexdigest())
    return frozenset(found)


def _ignore_patterns(path: Path) -> list[str]:
    """Pattern lines of one shipped ignore list. A missing, linked or
    unreadable file is an empty list: the ignore block is an offer, never a
    reason for the pack to fail."""
    if path.is_symlink() or not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return [line.strip() for line in text.splitlines()
            if pack_gitignore.canonical(line)]


def gitignore_lines(profile: str, source: Path | None = None,
                    user_dir: Path | None = None) -> list[str]:
    """The ignore lines the pack offers a project on ``profile``, in order.

    The base list (`GITIGNORE_FILE` beside `template/`, never inside it)
    then each loaded profile's own `GITIGNORE_FILE`, comments and blanks
    dropped, deduplicated on `pack_gitignore.canonical` keeping the first.
    Raises `PackRenderError` only for an unknown profile, as `render` would.
    """
    if not valid_profile_id(profile):
        raise PackRenderError(f"unknown profile {profile!r}")
    root = Path(source) if source is not None else vendor_dir()
    names = ["web", "ios"] if profile == "both" else [profile]
    profiles = [load_profile(name, root, user_dir) for name in names]
    lines = _ignore_patterns(root / GITIGNORE_FILE)
    for loaded in profiles:
        lines += _ignore_patterns(Path(loaded["_dir"]) / GITIGNORE_FILE)
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        form = pack_gitignore.canonical(line)
        if form in seen:
            continue
        seen.add(form)
        out.append(line)
    return out


def after_steps_for(profile: str, source: Path | None = None,
                    user_dir: Path | None = None) -> list[list[str]]:
    """The review section's after-steps for ``profile`` as `[id, label, how]`
    triples, ids unique across the profiles of a ``both`` install, first
    declaration winning. ``[]`` for an unknown or unreadable profile: the
    ledger row then offers the generic commit and push."""
    try:
        if not valid_profile_id(profile):
            return []
        root = Path(source) if source is not None else vendor_dir()
        names = ["web", "ios"] if profile == "both" else [profile]
        out: list[list[str]] = []
        seen: set[str] = set()
        for name in names:
            for step in load_profile(name, root, user_dir).get(
                    "after_steps") or []:
                if step[0] not in seen:
                    seen.add(step[0])
                    out.append(list(step))
        return out
    except PackRenderError:
        return []


def _managed_region(managed: bytes) -> bytes:
    body = managed.replace(b"\r\n", b"\n")
    if body and not body.endswith(b"\n"):
        body += b"\n"
    return (
        (BEGIN_MARK + "\n").encode("utf-8")
        + body
        + (END_MARK + "\n").encode("utf-8")
    )


def file_digest(text: bytes) -> str:
    """SHA-256 of a whole file, line endings normalised (see `managed_digest`)."""
    return hashlib.sha256(text.replace(b"\r\n", b"\n")).hexdigest()


def managed_digest(text: bytes) -> str:
    """SHA-256 of the managed region in ``text``, markers included.

    Line endings are normalised, so a checkout that converts to CRLF still
    reads as unedited. ``""`` when ``text`` carries no complete marker pair.
    """
    body = text.replace(b"\r\n", b"\n")
    begin = BEGIN_MARK.encode("utf-8")
    end = END_MARK.encode("utf-8")
    start = body.find(begin)
    stop = body.find(end)
    if start == -1 or stop == -1 or stop <= start:
        return ""
    region = body[start:stop + len(end)]
    return hashlib.sha256(region).hexdigest()


def splice(existing: bytes, managed: bytes) -> bytes:
    """Keep bytes outside the managed markers; never destructive."""
    region = _managed_region(managed)
    if not existing:
        return region
    begin = BEGIN_MARK.encode("utf-8")
    end = END_MARK.encode("utf-8")
    start = existing.find(begin)
    stop = existing.find(end)
    if start != -1 and stop != -1 and stop > start:
        line_end = existing.find(b"\n", stop)
        after = existing[line_end + 1:] if line_end != -1 else b""
        return existing[:start] + region + after
    old = existing.replace(b"\r\n", b"\n")
    if old and not old.endswith(b"\n"):
        old += b"\n"
    return region + b"\n" + old


def merge_settings(
    existing: bytes, managed: bytes, owned: list[str],
    owned_deny: list[str] | None = None,
) -> tuple[bytes, list[str], list[str]]:
    """Replace only previously-owned ``permissions.allow`` and ``deny`` rows.

    Returns ``(bytes, new_owned_allow, new_owned_deny)``. A project's own
    rows in either list survive; ``owned_deny`` of ``None`` (a ledger row
    written before the pack owned deny rows) reads as none owned, so the
    first resync adds the pack's deny rows and removes nothing.

    Unparseable existing JSON raises ``PackRenderError`` so the caller can
    leave that one file alone.
    """
    try:
        current = json.loads(existing.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackRenderError(
            "the project's .claude/settings.json is not valid JSON, "
            "so Dark Army left it alone"
        ) from exc
    if not isinstance(current, dict):
        raise PackRenderError(
            "the project's .claude/settings.json is not valid JSON, "
            "so Dark Army left it alone"
        )
    try:
        json.loads(managed.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackRenderError(
            "the rendered settings.json was not valid JSON"
        ) from exc
    new_owned = settings_allow_rows(managed)
    new_owned_deny = settings_deny_rows(managed)
    perms = current.get("permissions")
    if not isinstance(perms, dict):
        perms = {}
        current["permissions"] = perms
    perms["allow"] = _merge_owned_rows(perms.get("allow"), owned, new_owned)
    if new_owned_deny or owned_deny or "deny" in perms:
        perms["deny"] = _merge_owned_rows(
            perms.get("deny"), owned_deny or [], new_owned_deny)
    return ((json.dumps(current, indent=2) + "\n").encode("utf-8"),
            new_owned, new_owned_deny)


def _merge_owned_rows(existing, owned: list[str],
                      incoming: list[str]) -> list:
    """Drop the rows the pack owned last time, keep the project's, add ours."""
    if not isinstance(existing, list):
        existing = []
    owned_set = set(owned)
    kept = [row for row in existing if row not in owned_set]
    for row in incoming:
        if row not in kept:
            kept.append(row)
    return kept


def _permission_rows(managed: bytes, key: str) -> list[str]:
    try:
        incoming = json.loads(managed.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(incoming, dict):
        return []
    rows = (incoming.get("permissions") or {}).get(key) or []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, str)]


def settings_deny_rows(managed: bytes) -> list[str]:
    """The ``permissions.deny`` rows this pack writes, from rendered JSON."""
    return _permission_rows(managed, "deny")


def settings_allow_rows(managed: bytes) -> list[str]:
    """The ``permissions.allow`` rows this pack writes, from rendered JSON."""
    return _permission_rows(managed, "allow")


def render(
    profile: str,
    prefix: str,
    project: str,
    app: str = "",
    gitnexus_repo: str = "",
    source: Path | None = None,
    models=None,
    user_dir: Path | None = None,
    efforts=None,
) -> dict[str, bytes]:
    """Render one profile into a project-relative mapping. Writes nothing.

    ``profile`` is ``web`` / ``ios`` / ``both`` or any other profile folder's
    name; ``user_dir`` is where the person's own profiles live
    (``paths.USER_PROFILES_PATH``), ``None`` for shipped profiles only.

    ``models`` is the resolved ``{provider: {role: model}}`` table for this
    project (``agent_models.resolve``); each brief gets its Claude value as a
    ``model:`` line and each shim its own provider's value. ``None`` — or a
    table of empty cells — renders exactly what it did before the setting
    existed.

    ``efforts`` is the resolved ``{provider: {role: level}}`` effort table
    (``agent_models.resolve_efforts``): a chosen Claude level becomes an
    ``effort:`` line, a Grok level likewise, and a Codex shim carries its level
    (``SHIPPED_CODEX_EFFORT`` where there is no table), none where it is empty.
    ``None`` renders exactly what it did before the setting existed.
    """
    if not valid_profile_id(profile):
        raise PackRenderError(f"unknown profile {profile!r}")
    if not PREFIX_RE.fullmatch(prefix):
        raise PackRenderError(
            "prefix is 1-8 lowercase letters or digits, starting with a letter"
        )
    root = Path(source) if source is not None else vendor_dir()
    template = root / "template"
    if not (template / "CLAUDE.md").is_file():
        raise PackRenderError("the agent pack is missing from this install")
    names = ["web", "ios"] if profile == "both" else [profile]
    profiles = [load_profile(name, root, user_dir) for name in names]
    mapping = _read_tree(template)
    for loaded in profiles:
        _overlay_profile(loaded, mapping)
    keep_leads(mapping, lead_areas(profiles))
    mapping[".claude/skills/ship/templates/questions.md"] = write_questions(
        profiles)
    mapping, leftovers = substitute(
        mapping, build_substitutions(
            profiles, prefix, project, app=app, gitnexus_repo=gitnexus_repo))
    if leftovers:
        raise PackRenderError(
            "unresolved placeholders: " + ", ".join(leftovers[:8])
        )
    pin_worker_models(mapping, models)
    mirror_skills(mapping)
    pin_models(mapping, models, prefix)
    pin_efforts(mapping, efforts, prefix)
    shims(mapping, models, prefix, efforts)
    return mapping
