"""What the frozen app must carry, and what it must not.

`host/setup.py` names three lists py2app acts on: `packages` and `includes`
(what must ship) and `EXCLUDES` (build and test tools that must never ship,
whatever the build venv happens to hold). This file holds all three to the
code and to a built bundle.

The static half always runs. It reads `setup.py` with `ast` and never imports
it: importing it rewrites `_version_info.py` and calls `setup()`.

The bundle half opens `host/dist/Dark Army.app` and is skipped, with the
reason printed, when there is no build (CI) or the build is older than
`setup.py`, `requirements.txt`, `build.sh` or `launcher.py`. It compares no versions, so a venv upgrade
after a build cannot redden it; `tools/frozen_components.py manifest --check`
is the version-sensitive comparison and stays a command, not a test.

The bundle's interpreter is run as its own and nothing else's: the bare
`Contents/MacOS/python` is the host's Python and reads Homebrew's
site-packages, so a module present on this Mac would pass. With a clean
environment, `PYTHONHOME` at `Contents/Resources`, `-S` (no site-packages)
and `-P` (no current directory in front), `sys.path` is the bundle's
`python312.zip`, `lib/python3.12` and `lib-dynload` alone — the same library
the launcher's `__boot__.py` resolves. The app's main executable is
never started: it would launch a second live menu-bar app.

The bundle is signed, and one new or rewritten file under `Contents/`
breaks `codesign --verify --deep --strict`. `build.sh` therefore recompiles
every shipped module's cache as `unchecked-hash` (PEP 552) before signing —
a cache CPython loads without comparing it to the source and never rewrites,
whatever the file times — and `launcher.py` sets `sys.dont_write_bytecode`.
The copy test is the proof: a bundle copied the installer's way (`cp -R`,
which drops mtimes) and run with no `-B` and no `PYTHONDONTWRITEBYTECODE`
writes nothing inside itself and still verifies. The in-place smoke run
keeps `-B` all the same, so a regression fails a test instead of breaking
`host/dist`'s seal. The guard is a snapshot of every path, size and mtime
under `Contents/` taken before the run and compared after it: a byte-level
check of the test's own footprint, independent of whether the seal was
already intact when the test began.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import marshal
import re
import subprocess
import zipfile
from pathlib import Path

import pytest

HOST = Path(__file__).resolve().parents[1]
REPO = HOST.parent
SETUP = HOST / "setup.py"
REQUIREMENTS = HOST / "requirements.txt"
BUILD_SH = HOST / "build.sh"
LAUNCHER = HOST / "launcher.py"
APP = HOST / "dist" / "Dark Army.app"
RESOURCES = APP / "Contents" / "Resources"
LIB = RESOURCES / "lib"
TREE = LIB / "python3.12"
ZIP = LIB / "python312.zip"
DYNLOAD = TREE / "lib-dynload"
INTERPRETER = APP / "Contents" / "MacOS" / "python"
GENERATOR = REPO / "tools" / "frozen_components.py"

#: The exclusions this file insists on; `EXCLUDES` may hold more.
REQUIRED_EXCLUDES = frozenset({
    "numpy", "pytest", "_pytest", "pluggy", "iniconfig", "pygments",
    "setuptools", "pkg_resources", "_distutils_hack",
})

#: Distributions whose dist-info must not ship: the test and build tools, and
#: what setuptools vendors. `typing_extensions` is vendored there too but is
#: kept on purpose (filelock and cryptography import it), so it is not here.
EXCLUDED_DISTS = frozenset({
    "numpy", "pytest", "pluggy", "iniconfig", "pygments", "setuptools",
    "packaging", "more_itertools", "importlib_metadata", "zipp",
    "platformdirs", "tomli", "wheel", "autocommand", "backports_tarfile",
})

#: Where each `includes` entry comes from in `host/requirements.txt`.
INCLUDE_SOURCES = {
    "Quartz": "pyobjc-framework-Quartz",
    "rumps": "rumps",
    "websockets": "websockets",
}
#: `objc` arrives through rumps (pyobjc-core) and `_cffi_backend` through
#: cryptography (cffi): transitive, so they name no requirement line.
TRANSITIVE_INCLUDES = frozenset({"objc", "_cffi_backend"})

#: The app's own modules the smoke import must load; `relay` and `relay_ws`
#: are named because their imports are the lazy, launch-time-invisible ones.
APP_MODULES = (
    "dark_army_daemon.daemon",
    "dark_army_menubar.app",
    "dark_army_daemon.relay",
    "dark_army_daemon.relay_ws",
)


def _norm(name: str) -> str:
    return re.sub(r"[-.]", "_", name).lower()


# ---------------------------------------------------------------- setup.py

def _setup_tree() -> ast.Module:
    return ast.parse(SETUP.read_text(encoding="utf-8"))


def _assigned(name: str) -> ast.expr:
    for node in _setup_tree().body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return node.value
    raise AssertionError(f"host/setup.py assigns no {name}")


def _excludes() -> list[str]:
    value = _assigned("EXCLUDES")
    assert isinstance(value, ast.List), "EXCLUDES must be a list literal"
    return ast.literal_eval(value)


def _option(key: str) -> ast.expr:
    options = _assigned("OPTIONS")
    assert isinstance(options, ast.Dict)
    for k, v in zip(options.keys, options.values):
        if isinstance(k, ast.Constant) and k.value == key:
            return v
    raise AssertionError(f"OPTIONS has no {key!r}")


def _shipped_names() -> list[str]:
    return list(ast.literal_eval(_option("packages"))) + \
        list(ast.literal_eval(_option("includes")))


def _includes() -> list[str]:
    return list(ast.literal_eval(_option("includes")))


def _requirement_names() -> set[str]:
    # The same parse as test_third_party_notices._requirement_names.
    names = set()
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name = re.split(r"[\s<>=!~;\[]", line, maxsplit=1)[0]
            if name:
                names.add(name.lower())
    return names


def test_setup_declares_the_excludes():
    excludes = _excludes()
    assert all(isinstance(n, str) for n in excludes)
    assert REQUIRED_EXCLUDES <= set(excludes), \
        sorted(REQUIRED_EXCLUDES - set(excludes))
    value = _option("excludes")
    assert isinstance(value, ast.Name) and value.id == "EXCLUDES"


def test_nothing_is_both_excluded_and_shipped():
    excludes = set(_excludes())
    shipped = {name.split(".")[0] for name in _shipped_names()}
    assert shipped & excludes == set()
    assert "typing_extensions" not in excludes


def _runtime_sources() -> list[Path]:
    files = [HOST / "launcher.py"]
    for package in sorted(HOST.glob("dark_army_*")):
        if package.is_dir():
            files += sorted(package.rglob("*.py"))
    return files


def test_no_runtime_module_imports_an_excluded_name():
    excludes = set(_excludes())
    sources = _runtime_sources()
    assert len(sources) > 10, "found no runtime modules to walk"
    offenders = []
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                if name.split(".")[0] in excludes:
                    offenders.append(f"{path.relative_to(HOST)}:{node.lineno} {name}")
    assert offenders == []


def test_every_include_has_a_declared_source():
    includes = set(_includes())
    assert includes == set(INCLUDE_SOURCES) | TRANSITIVE_INCLUDES, \
        "a new `includes` entry needs its source named here"
    requirements = _requirement_names()
    missing = [f"{module} -> {dist}" for module, dist in INCLUDE_SOURCES.items()
               if dist.lower() not in requirements]
    assert missing == []


def _names_dark_army(node: ast.stmt) -> bool:
    if isinstance(node, ast.Import):
        return any(a.name.startswith("dark_army_") for a in node.names)
    if isinstance(node, ast.ImportFrom):
        return (node.module or "").startswith("dark_army_")
    return False


def test_launcher_never_writes_bytecode():
    body = ast.parse(LAUNCHER.read_text(encoding="utf-8")).body
    first_import = next(i for i, node in enumerate(body) if _names_dark_army(node))
    flagged = [
        i for i, node in enumerate(body)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "dont_write_bytecode"
                and isinstance(t.value, ast.Name) and t.value.id == "sys"
                for t in node.targets)
        and isinstance(node.value, ast.Constant) and node.value.value is True
    ]
    assert flagged, "launcher.py never sets sys.dont_write_bytecode = True"
    assert flagged[0] < first_import, \
        "sys.dont_write_bytecode must be set before Dark Army's first import"


def test_build_recompiles_the_bundle_before_signing():
    text = BUILD_SH.read_text(encoding="utf-8")
    assert "--invalidation-mode unchecked-hash" in text
    assert "-m compileall" in text
    assert '-s "$SCRIPT_DIR/dist/"' in text
    at = text.index("compileall")
    assert text.index("setup.py py2app") < at < text.index('echo "==> Signing..."')


# ---------------------------------------------------------------- the bundle

def _bundle_skip_reason() -> str:
    plist = APP / "Contents" / "Info.plist"
    if not plist.is_file():
        return f"no built app at {APP.relative_to(REPO)} (build it with ./build.sh)"
    built = plist.stat().st_mtime
    # build.sh and launcher.py too: the bytecode rules below are theirs, and a
    # bundle built by an older script would only show that it predates them.
    for source in (SETUP, REQUIREMENTS, BUILD_SH, LAUNCHER):
        if source.stat().st_mtime > built:
            return (f"the built app predates {source.relative_to(REPO)}; "
                    "rebuild with ./build.sh --allow-untagged")
    return ""


bundle = pytest.mark.skipif(bool(_bundle_skip_reason()),
                            reason=_bundle_skip_reason() or "bundle present")


def _module_stem(entry: str) -> str:
    # `foo/…`, `foo.pyc`, `foo.py`, `foo.cpython-312-darwin.so`, `foo.so`.
    return entry.split("/", 1)[0].split(".", 1)[0]


def _zip_tops() -> set[str]:
    with zipfile.ZipFile(ZIP) as zf:
        return {n.split("/", 1)[0] for n in zf.namelist()}


def _module_names_in(entries) -> set[str]:
    return {_module_stem(e) for e in entries if ".dist-info" not in e}


@bundle
def test_bundle_carries_none_of_the_excludes():
    excludes = {_norm(n) for n in _excludes()}
    in_zip = {_norm(n) for n in _module_names_in(_zip_tops())}
    in_tree = {_norm(n) for n in _module_names_in(p.name for p in TREE.iterdir())}
    assert in_zip & excludes == set()
    assert in_tree & excludes == set()
    # Extensions nest: lib-dynload/Quartz/CoreGraphics/_callbacks.so, …
    in_dynload = {_norm(_module_stem(p.relative_to(DYNLOAD).as_posix()))
                  for p in DYNLOAD.rglob("*.so")}
    assert in_dynload, "lib-dynload holds no extension at all"
    assert in_dynload & excludes == set()

    spec = importlib.util.spec_from_file_location("frozen_components", GENERATOR)
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    dists = generator.python_dists(APP)
    assert dists, "the bundle carries no dist-info at all"
    shipped = []
    for (name, version), entry in dists.items():
        n = _norm(name)
        if n in EXCLUDED_DISTS or n.startswith("jaraco") or \
                "setuptools/_vendor" in entry["where"]:
            shipped.append(f"{name} {version}")
    assert shipped == []


def _resolves_in_bundle(module: str, zip_tops: set[str]) -> bool:
    if module in _module_names_in(zip_tops):
        return True
    for where in (TREE, DYNLOAD):
        if any(_module_stem(p.name) == module for p in where.iterdir()):
            return True
    return False


@bundle
def test_bundle_carries_every_include():
    tops = _zip_tops()
    missing = [m for m in _includes() if not _resolves_in_bundle(m, tops)]
    assert missing == []


SMOKE = r"""
import json, sys
out = {"path": list(sys.path), "imported": [], "failed": {}, "absent": {}}
for name in sys.argv[1].split(","):
    try:
        __import__(name)
        out["imported"].append(name)
    except BaseException as exc:
        out["failed"][name] = f"{type(exc).__name__}: {exc}"
try:
    import Quartz
    out["quartz_event"] = Quartz.CGEventCreateKeyboardEvent(None, 0, True) is not None
except BaseException as exc:
    out["quartz_event"] = f"{type(exc).__name__}: {exc}"
for name in sys.argv[2].split(","):
    try:
        __import__(name)
        out["absent"][name] = "imported"
    except ModuleNotFoundError:
        out["absent"][name] = "ModuleNotFoundError"
    except BaseException as exc:
        out["absent"][name] = f"{type(exc).__name__}: {exc}"
print(json.dumps(out))
"""


def _contents_snapshot(app: Path = APP) -> dict[str, tuple[int, int]]:
    contents = app / "Contents"
    return {str(p.relative_to(contents)): (p.lstat().st_size, p.lstat().st_mtime_ns)
            for p in contents.rglob("*")}


@bundle
def test_frozen_interpreter_loads_the_app_from_the_bundle_alone():
    assert INTERPRETER.is_file(), f"no interpreter at {INTERPRETER}"
    wanted = list(APP_MODULES) + _includes()
    absent = sorted(REQUIRED_EXCLUDES)
    env = {
        "HOME": str(Path.home()),
        "PATH": "/usr/bin:/bin",
        "PYTHONHOME": str(RESOURCES),
        "PYTHONNOUSERSITE": "1",
        # With -B: a .pyc written inside the signed bundle breaks its seal.
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    before = _contents_snapshot()
    proc = subprocess.run(
        [str(INTERPRETER), "-B", "-S", "-P", "-c", SMOKE,
         ",".join(wanted), ",".join(absent)],
        env=env, timeout=60, capture_output=True, text=True)
    after = _contents_snapshot()
    changed = sorted(k for k in before.keys() | after.keys()
                     if before.get(k) != after.get(k))
    assert changed == [], f"the smoke run wrote inside the bundle: {changed[:10]}"
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    outside = [p for p in out["path"]
               if not str(Path(p)).startswith(str(RESOURCES) + "/")]
    assert outside == [], "the interpreter read a library outside the bundle"
    assert out["failed"] == {}
    assert sorted(out["imported"]) == sorted(wanted)
    assert out["quartz_event"] is True
    assert out["absent"] == {name: "ModuleNotFoundError" for name in absent}


# ------------------------------------------------ the bundle's bytecode

CACHE_TAG = "cpython-312"
#: PEP 552: bit 0 says hash-based, bit 1 says check the source. `0b01` is an
#: unchecked-hash cache, the one kind CPython never regenerates.
UNCHECKED_HASH = 0b01


def _shipped_sources() -> list[Path]:
    return sorted(p for p in TREE.rglob("*.py")
                  if "lib-dynload" not in p.relative_to(TREE).parts
                  and "__pycache__" not in p.parts)


def _cache_for(source: Path) -> Path:
    return source.parent / "__pycache__" / f"{source.stem}.{CACHE_TAG}.pyc"


@bundle
def test_every_shipped_module_has_unchecked_hash_bytecode():
    sources = _shipped_sources()
    assert len(sources) > 100, "found too few shipped modules to judge"
    offenders = []
    for source in sources:
        where = source.relative_to(TREE)
        cache = _cache_for(source)
        if not cache.is_file():
            offenders.append(f"{where}: no cache")
            continue
        head = cache.read_bytes()[:16]
        if head[:4] != importlib.util.MAGIC_NUMBER:
            offenders.append(f"{where}: magic {head[:4].hex()}")
        elif int.from_bytes(head[4:8], "little") & 0b11 != UNCHECKED_HASH:
            offenders.append(f"{where}: flags {int.from_bytes(head[4:8], 'little')}")
        elif head[8:16] != importlib.util.source_hash(source.read_bytes()):
            offenders.append(f"{where}: hash does not match the source")
    assert offenders == []


@bundle
def test_version_bytecode_matches_the_stamped_source():
    package = TREE / "dark_army_menubar"
    source = package / "_version_info.py"
    assigned = [node for node in ast.parse(source.read_text(encoding="utf-8")).body
                if isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "VERSION"
                        for t in node.targets)]
    assert assigned, "_version_info.py assigns no VERSION"
    stamped = ast.literal_eval(assigned[0].value)
    code = marshal.loads(_cache_for(source).read_bytes()[16:])
    assert stamped in code.co_consts, \
        f"the cache carries {code.co_consts!r}, the source says {stamped!r}"


def _pyc_code(path: Path):
    return marshal.loads(path.read_bytes()[16:])


@bundle
def test_no_orphan_bytecode_ships():
    orphans = []
    for cache in sorted(TREE.rglob("__pycache__/*.pyc")):
        stem = cache.name.split(".", 1)[0]
        if not (cache.parent.parent / f"{stem}.py").is_file():
            orphans.append(str(cache.relative_to(TREE)))
    assert orphans == []

    homes = []
    for cache in sorted(TREE.rglob("*.pyc")):
        name = _pyc_code(cache).co_filename
        if name.startswith(("/Users/", "/home/")):
            homes.append(f"{cache.relative_to(TREE)}: {name}")
    assert homes == [], homes[:5]


COPY_PROBE = r"""
import json, sys
out = {"flags": sys.flags.dont_write_bytecode, "attr": sys.dont_write_bytecode,
       "path": list(sys.path), "imported": [], "failed": {}}
for name in sys.argv[1].split(","):
    try:
        __import__(name)
        out["imported"].append(name)
    except BaseException as exc:
        out["failed"][name] = f"{type(exc).__name__}: {exc}"
print(json.dumps(out))
"""


@bundle
def test_a_copied_bundle_is_never_rewritten(tmp_path):
    # The installer's copy: plain `cp -R`, which gives every file a fresh
    # mtime, so a timestamp cache would be out of date everywhere.
    copy = tmp_path / APP.name
    subprocess.run(["/bin/cp", "-R", str(APP), str(copy)], check=True, timeout=120)
    resources = copy / "Contents" / "Resources"
    wanted = list(APP_MODULES) + ["dark_army_menubar._version_info"]
    # No -B and no PYTHONDONTWRITEBYTECODE: this run is unguarded on purpose.
    env = {
        "HOME": str(Path.home()),
        "PATH": "/usr/bin:/bin",
        "PYTHONHOME": str(resources),
        "PYTHONNOUSERSITE": "1",
    }
    before = _contents_snapshot(copy)
    proc = subprocess.run(
        [str(copy / "Contents" / "MacOS" / "python"), "-S", "-P", "-c",
         COPY_PROBE, ",".join(wanted)],
        env=env, timeout=120, capture_output=True, text=True)
    after = _contents_snapshot(copy)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["flags"] == 0 and out["attr"] is False, \
        "the run was guarded, so it proves nothing"
    outside = [p for p in out["path"]
               if not str(Path(p)).startswith(str(resources) + "/")]
    assert outside == [], "the copy read a library outside itself"
    assert out["failed"] == {}
    assert sorted(out["imported"]) == sorted(wanted)

    changed = sorted(k for k in before.keys() | after.keys()
                     if before.get(k) != after.get(k))
    assert changed == [], f"the copy wrote inside itself: {changed[:10]}"
    seal = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(copy)],
        timeout=120, capture_output=True, text=True)
    assert seal.returncode == 0, seal.stderr[-2000:]
