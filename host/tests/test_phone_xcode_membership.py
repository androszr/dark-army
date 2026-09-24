# host/tests/test_phone_xcode_membership.py
"""Every iOS Swift file on disk is a member of the right Xcode target.

AgentReportView.swift fell out of the BobPhone target on 2026-09-10 and
the phone's other checks stayed green, because they read files from disk.
A comment-string check would miss the ten uncommented one-line
PBXBuildFile entries the test targets use, so this module walks the
project records instead.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IOS = ROOT / "ios"
PBXPROJ = IOS / "BobPhone.xcodeproj" / "project.pbxproj"

_OBJECT_START = re.compile(r"^[ \t]*([0-9A-Za-z]+)\s*=\s*\{", re.M)
_ID_TOKEN = re.compile(r"[0-9A-Za-z]+")


def parse_objects(text: str) -> dict[str, str]:
    """Map each pbxproj object id to its brace body.

    Comments are stripped first. Nested ``buildSettings`` / TargetAttributes
    blocks have an id-shaped key but no ``isa = ``, and are dropped.
    """
    stripped = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    objects: dict[str, str] = {}
    for match in _OBJECT_START.finditer(stripped):
        start = match.end() - 1
        depth = 0
        for index, char in enumerate(stripped[start:], start):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    body = stripped[start + 1:index]
                    if "isa = " in body:
                        objects[match.group(1)] = body
                    break
    return objects


def _isa(body: str) -> str:
    return _field(body, "isa")


def _field(body: str, key: str) -> str:
    match = re.search(
        rf"(?:^|[\s;]){re.escape(key)}\s*=\s*(\"[^\"]*\"|[^\s;]+)\s*;",
        body,
    )
    if match is None:
        return ""
    value = match.group(1)
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _id_list(body: str, key: str) -> list[str]:
    match = re.search(
        rf"(?:^|[\s;]){re.escape(key)}\s*=\s*\((.*?)\)",
        body,
        flags=re.S,
    )
    if match is None:
        return []
    return _ID_TOKEN.findall(match.group(1))


def sources_by_target(text: str) -> dict[str, frozenset[str]]:
    """ios-relative paths compiled by each PBXNativeTarget's Sources phase."""
    objects = parse_objects(text)
    parent: dict[str, str] = {}
    for obj_id, body in objects.items():
        if _isa(body) != "PBXGroup":
            continue
        for child in _id_list(body, "children"):
            parent[child] = obj_id

    def _ref_path(ref: str) -> str:
        body = objects.get(ref)
        if body is None:
            raise LookupError(f"unresolved id {ref}")
        path = _field(body, "path")
        tree = _field(body, "sourceTree")
        # SOURCE_ROOT is the committed ios-relative path; do not walk groups.
        if tree == "SOURCE_ROOT":
            if not path:
                raise LookupError(f"unresolved id {ref}")
            return path
        if tree != "<group>":
            raise LookupError(f"unresolved id {ref}")
        parts: list[str] = []
        current: str | None = ref
        own = True
        while current is not None:
            node = objects.get(current)
            if node is None:
                raise LookupError(f"unresolved id {current}")
            piece = path if own else _field(node, "path")
            own = False
            if piece:
                parts.append(piece)
            current = parent.get(current)
        if not parts:
            raise LookupError(f"unresolved id {ref}")
        parts.reverse()
        return "/".join(parts)

    sources: dict[str, set[str]] = {}
    for obj_id, body in objects.items():
        if _isa(body) != "PBXNativeTarget":
            continue
        name = _field(body, "name")
        if not name:
            raise LookupError(f"unresolved id {obj_id}")
        paths: set[str] = set()
        for phase_id in _id_list(body, "buildPhases"):
            phase = objects.get(phase_id)
            if phase is None:
                raise LookupError(f"unresolved id {phase_id}")
            if _isa(phase) != "PBXSourcesBuildPhase":
                continue
            for file_id in _id_list(phase, "files"):
                build = objects.get(file_id)
                if build is None or _isa(build) != "PBXBuildFile":
                    raise LookupError(f"unresolved id {file_id}")
                ref = _field(build, "fileRef")
                if not ref:
                    raise LookupError(f"unresolved id {file_id}")
                paths.add(_ref_path(ref))
        sources[name] = paths
    return {name: frozenset(paths) for name, paths in sources.items()}


REQUIRED_TARGETS: dict[str, frozenset[str]] = {
    "BobPhone": frozenset({"BobPhone"}),
    "BobPhoneTests": frozenset({"BobPhoneTests"}),
    "BobPhoneWidget": frozenset({"BobPhoneWidget"}),
    "BobPhoneWidgetTests": frozenset({"BobPhoneWidgetTests"}),
    "BobPhoneNotification": frozenset({"BobPhoneNotification"}),
    "Shared": frozenset({"BobPhone", "BobPhoneWidget"}),
}
EXPECTED_TARGET_NAMES = frozenset(
    t for ts in REQUIRED_TARGETS.values() for t in ts
)
# ios-relative paths kept out of the project on purpose. An entry must
# name a file that is still on disk, or membership_problems reports it.
INTENTIONALLY_UNBUILT: frozenset[str] = frozenset()


def membership_problems(
    text: str,
    on_disk: frozenset[str],
    *,
    unbuilt: frozenset[str] = INTENTIONALLY_UNBUILT,
) -> list[str]:
    """Compare the project file to the files on disk. Extra-target membership
    is never a problem; missing a required target is.
    """
    sources = sources_by_target(text)
    problems: list[str] = []
    for name in sorted(EXPECTED_TARGET_NAMES):
        if name not in sources:
            problems.append(f"missing target {name}")
    for path in sorted(on_disk):
        if path in unbuilt:
            continue
        required = REQUIRED_TARGETS.get(path.split("/", 1)[0])
        if required is None:
            continue
        for target in sorted(required):
            if path not in sources.get(target, frozenset()):
                problems.append(f"{path} is not in target {target}")
    for target in sorted(sources):
        for path in sorted(sources[target]):
            if path not in on_disk:
                problems.append(
                    f"{target} names {path} which is not on disk"
                )
    for path in sorted(unbuilt):
        if path not in on_disk:
            problems.append(
                f"INTENTIONALLY_UNBUILT names {path} which is not on disk"
            )
    return problems


def swift_files_on_disk() -> frozenset[str]:
    return frozenset(
        p.relative_to(IOS).as_posix()
        for p in sorted(IOS.glob("**/*.swift"))
    )


_REAL_TEXT = PBXPROJ.read_text()
_REAL_DISK = swift_files_on_disk()


def _drop_build_file(text: str, filename: str) -> str:
    """Remove *filename*'s PBXBuildFile object and its Sources-phase entry."""
    build_id = None
    for line in text.splitlines():
        if filename in line and "PBXBuildFile" in line:
            match = re.match(r"[ \t]*([0-9A-Za-z]+)\b", line)
            assert match is not None, filename
            build_id = match.group(1)
            break
    assert build_id is not None, filename
    kept: list[str] = []
    for line in text.splitlines(keepends=True):
        token = re.match(r"[ \t]*([0-9A-Za-z]+)\b", line)
        if token is not None and token.group(1) == build_id:
            continue
        kept.append(line)
    return "".join(kept)


def test_parser_finds_every_target_and_todays_shape():
    sources = sources_by_target(_REAL_TEXT)
    assert set(sources) == EXPECTED_TARGET_NAMES
    assert "Shared/FleetSummary.swift" in sources["BobPhone"]
    assert "Shared/FleetSummary.swift" in sources["BobPhoneWidget"]
    assert "BobPhoneTests/ActionReplyTests.swift" in sources["BobPhoneTests"]
    assert (
        "BobPhoneWidgetTests/WidgetTileTests.swift"
        in sources["BobPhoneWidgetTests"]
    )
    assert "BobPhone/AgentReportView.swift" in sources["BobPhone"]


def test_a_file_dropped_from_the_project_is_reported():
    mutated = _drop_build_file(_REAL_TEXT, "AgentReportView.swift")
    assert membership_problems(mutated, _REAL_DISK) == [
        "BobPhone/AgentReportView.swift is not in target BobPhone",
    ]


def test_a_file_deleted_from_disk_is_reported():
    disk = _REAL_DISK - {"BobPhone/AgentReportView.swift"}
    assert membership_problems(_REAL_TEXT, disk) == [
        "BobPhone names BobPhone/AgentReportView.swift which is not on disk",
    ]


def test_a_stale_exception_is_reported():
    assert membership_problems(
        _REAL_TEXT, _REAL_DISK, unbuilt=frozenset({"BobPhone/Gone.swift"}),
    ) == [
        "INTENTIONALLY_UNBUILT names BobPhone/Gone.swift which is not on disk",
    ]


def test_an_exception_silences_only_its_own_file():
    mutated = _drop_build_file(_REAL_TEXT, "AgentReportView.swift")
    assert membership_problems(
        mutated,
        _REAL_DISK,
        unbuilt=frozenset({"BobPhone/AgentReportView.swift"}),
    ) == []


def test_a_project_with_no_targets_does_not_pass():
    problems = membership_problems("// objects = {}", _REAL_DISK)
    assert problems[:4] == [
        "missing target BobPhone",
        "missing target BobPhoneNotification",
        "missing target BobPhoneTests",
        "missing target BobPhoneWidget",
    ]


def test_every_phone_swift_file_is_in_its_target():
    problems = membership_problems(PBXPROJ.read_text(), swift_files_on_disk())
    assert problems == [], problems


def test_todays_counts_are_the_disk_counts():
    files = swift_files_on_disk()
    for folder in REQUIRED_TARGETS:
        present = [p for p in files if p.split("/", 1)[0] == folder]
        assert present, f"{folder} has no Swift files on disk"
