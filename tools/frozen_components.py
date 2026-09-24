"""What the frozen app carries, and the page that credits it.

`host/build.sh` freezes the menu-bar app with py2app, and py2app's import scan
brings in more than `host/requirements.txt` names (what the direct
dependencies pull in), held back from build and test tools by `host/setup.py`'s
`EXCLUDES`, plus the native libraries the Python runtime links (OpenSSL,
mpdecimal, liblzma, SQLite). This tool reads a *built* bundle, never the
requirements, so the list is what actually ships:

    host/.venv/bin/python tools/frozen_components.py manifest
        rewrite host/tests/data/frozen-components.txt from the bundle
    host/.venv/bin/python tools/frozen_components.py manifest --check
        exit 1 when the bundle and the checked-in manifest differ
    host/.venv/bin/python tools/frozen_components.py notices
        rewrite THIRD_PARTY_NOTICES.md from the bundle

Build first (`cd host && ./build.sh --allow-untagged`; never `--install`).
`--bundle` names another app. Run it under `host/.venv/bin/python`: the py2app
version and the Python licence text are read from that interpreter. Every
licence text is copied from the bundle's own dist-info, the SwiftPM checkout or
the Homebrew keg the native library was copied from — never retyped.
`host/tests/test_third_party_notices.py` holds the page to the manifest.
"""

from __future__ import annotations

import argparse
import email.parser
import importlib.metadata
import json
import plistlib
import re
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BUNDLE = REPO / "host" / "dist" / "Dark Army.app"
MANIFEST = REPO / "host" / "tests" / "data" / "frozen-components.txt"
NOTICES = REPO / "THIRD_PARTY_NOTICES.md"
REQUIREMENTS = REPO / "host" / "requirements.txt"
RESOLVED = REPO / "panel" / "Package.resolved"
CHECKOUTS = REPO / "panel" / ".build" / "checkouts"
BREW = Path("/opt/homebrew/opt")

#: Native libraries in `Contents/Frameworks`: which component each belongs to,
#: the Homebrew formula it was copied from, its licence and the file quoted.
#: A dylib not listed here stops the tool: add its row, do not skip it.
NATIVE = {
    "libssl.3.dylib": ("OpenSSL", "openssl@3", "Apache-2.0", ["LICENSE.txt"]),
    "libcrypto.3.dylib": ("OpenSSL", "openssl@3", "Apache-2.0", ["LICENSE.txt"]),
    "libmpdec.4.0.1.dylib": ("mpdecimal", "mpdecimal", "BSD-2-Clause", ["COPYRIGHT.txt"]),
    "liblzma.5.dylib": ("liblzma (XZ Utils)", "xz", "0BSD", ["COPYING.0BSD"]),
    "libsqlite3.3.53.4.dylib": ("SQLite", "sqlite", "Public domain", ["include/sqlite3.h:11"]),
}

#: Why a Python distribution is in the bundle, where the answer is not the
#: default ("py2app's import scan").
WHY = {
    "pyobjc-core": "pulled in by rumps",
    "pyobjc-framework-Cocoa": "pulled in by rumps",
    "cffi": "pulled in by cryptography; `host/setup.py` `includes` (`_cffi_backend`)",
    "pycparser": "pulled in by cffi",
}
SCAN = ("frozen in by py2app's import scan; Dark Army's own code does not "
        "import it")

#: Swift packages resolved for the panel, and whether they reach the app.
SWIFT = {
    "swiftterm": ("SwiftTerm", "MIT, with the xterm.js and SourceLair notices its "
                  "licence file carries", "the hosted terminal pane; linked into `BobPanel`"),
    "swift-argument-parser": ("swift-argument-parser", "Apache-2.0 (with the Swift "
                              "runtime library exception)", "resolved at build time for a "
                              "SwiftTerm tool target; not linked into `BobPanel` and not in "
                              "the app. Listed so the resolved set is complete"),
}


def _requirements() -> set[str]:
    names = set()
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.add(re.split(r"[\s<>=!~;\[]", line, maxsplit=1)[0].lower())
    return names


def _licence_file(name: str) -> bool:
    base = name.rsplit("/", 1)[-1].upper()
    return (bool(re.match(r"(LICEN[CS]E|COPYING)", base)) or "/licenses/" in name) \
        and not name.endswith("/")


def python_dists(bundle: Path) -> dict[tuple[str, str], dict]:
    """Every dist-info in the bundle, keyed (name, version): its metadata, its
    licence files and where it sits (top level or inside setuptools)."""
    found: dict[tuple[str, str], dict] = {}

    def add(prefix: str, read, names: list[str]) -> None:
        meta = email.parser.Parser().parsestr(read(prefix + "METADATA"))
        key = (meta["Name"], meta["Version"])
        entry = found.setdefault(key, {"meta": meta, "texts": [], "where": set()})
        entry["where"].add("setuptools/_vendor" if "setuptools/_vendor/" in prefix else "top")
        if not entry["texts"]:
            for n in sorted(names):
                if n.startswith(prefix) and _licence_file(n):
                    entry["texts"].append((n[len(prefix):], read(n).rstrip()))

    lib = bundle / "Contents" / "Resources" / "lib"
    with zipfile.ZipFile(lib / "python312.zip") as zf:
        names = zf.namelist()
        def read(n: str) -> str:
            return zf.read(n).decode("utf-8", "replace")
        for n in names:
            if n.endswith(".dist-info/METADATA"):
                add(n[: -len("METADATA")], read, names)
    tree = lib / "python3.12"
    files = [p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file()]
    for n in files:
        if n.endswith(".dist-info/METADATA"):
            add(n[: -len("METADATA")],
                lambda m: (tree / m).read_text(encoding="utf-8", errors="replace"), files)
    return found


def native_libs(bundle: Path) -> list[str]:
    libs = sorted(p.name for p in (bundle / "Contents" / "Frameworks").glob("*.dylib")
                  if not p.is_symlink())
    unknown = [n for n in libs if n not in NATIVE]
    if unknown:
        sys.exit(f"frozen_components: add {unknown} to NATIVE before crediting")
    return libs


def runtime_version(bundle: Path) -> str:
    plist = bundle / "Contents/Frameworks/Python.framework/Versions/Current/Resources/Info.plist"
    return plistlib.loads(plist.read_bytes())["CFBundleVersion"]


def manifest_lines(bundle: Path) -> list[str]:
    lines = [f"python\t{n}\t{v}" for n, v in sorted(python_dists(bundle), key=lambda k: (k[0].lower(), k[1]))]
    lines += [f"native\t{n}" for n in native_libs(bundle)]
    lines.append(f"runtime\tPython\t{runtime_version(bundle)}")
    lines.append("bootstrap\tpy2app")
    return lines


def write_manifest(bundle: Path, check: bool) -> int:
    body = ("# What the frozen app carries, read off a built bundle by\n"
            "# `host/.venv/bin/python tools/frozen_components.py manifest`.\n"
            "# Refresh after a dependency change; THIRD_PARTY_NOTICES.md must credit\n"
            "# every line (test_third_party_notices.py).\n"
            + "\n".join(manifest_lines(bundle)) + "\n")
    if check:
        same = MANIFEST.is_file() and MANIFEST.read_text(encoding="utf-8") == body
        print("manifest in step" if same else "manifest differs from the bundle")
        return 0 if same else 1
    MANIFEST.write_text(body, encoding="utf-8")
    print(f"wrote {MANIFEST.relative_to(REPO)}")
    return 0


def _url(meta) -> str:
    if meta.get("Home-page"):
        return meta.get("Home-page")
    urls = [u.split(",", 1) for u in meta.get_all("Project-URL") or []]
    for key in ("homepage", "source", "source code", "repository", "code", "github"):
        for k, v in urls:
            if k.strip().lower() == key:
                return v.strip()
    return urls[0][1].strip() if urls else "not stated in its metadata"


def _licence_id(meta) -> str:
    if meta.get("License-Expression"):
        return meta.get("License-Expression")
    text = (meta.get("License") or "").strip()
    if text and "\n" not in text and len(text) < 80:
        return text
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or []
                   if c.startswith("License ::")]
    return " / ".join(classifiers) or "see the licence text below"


def _details(out: list[str], name: str, text: str) -> None:
    out += [f"<details><summary>{name}</summary>\n", "```text", text, "```\n",
            "</details>\n"]


#: The top of `THIRD_PARTY_NOTICES.md`, line by line, before the first
#: component section. `test_third_party_notices.py` holds the page to it.
HEADER = [
    "# Third-party notices\n",
    "Dark Army is built on other people's work. This page credits every",
    "outside component the installed app ships, with the licence each is",
    "distributed under. It is generated from a built app by",
    "`tools/frozen_components.py`, and the licence texts are copied from each",
    "package's metadata inside the app, the panel's source checkouts or the",
    "library the build copied, not retyped. Dark Army's own licence is",
    "`LICENSE`; both files travel inside the app bundle, under",
    "`Contents/Resources/`.\n",
]


def write_notices(bundle: Path) -> int:
    dists = python_dists(bundle)
    direct = _requirements()
    out = [*HEADER,
           "## The Python bundle\n",
           "The menu-bar app and the daemon are frozen by py2app into",
           "`Dark Army.app`. Every Python distribution inside it is listed here, at",
           "the version the build froze, including the ones py2app's import scan",
           "brought in that Dark Army never imports. `host/requirements.txt` states",
           "the minimums of the direct dependencies.\n"]
    cocoa = next((e for (n, _), e in dists.items() if n == "pyobjc-framework-Cocoa"), None)
    for (name, version), entry in sorted(dists.items(), key=lambda kv: (kv[0][0].lower(), kv[0][1])):
        meta = entry["meta"]
        why = "`host/requirements.txt`" if name.lower() in direct else WHY.get(name, SCAN)
        if entry["where"] == {"setuptools/_vendor"}:
            why = "vendored inside setuptools"
        elif "setuptools/_vendor" in entry["where"]:
            why += "; also vendored inside setuptools"
        out += [f"### {name} {version}\n", f"- Licence: {_licence_id(meta)}",
                f"- Upstream: {_url(meta)}", f"- Why it is here: {why}\n"]
        texts = entry["texts"]
        if not texts and name == "pyobjc-core" and cocoa:
            out.append("pyobjc-core's wheel ships no licence file; the text below is the "
                       "one the same PyObjC project ships in pyobjc-framework-Cocoa's "
                       "metadata.\n")
            texts = cocoa["texts"]
        if not texts:
            sys.exit(f"frozen_components: {name} {version} carries no licence text")
        for fname, text in texts:
            _details(out, fname, text)
    py2app = importlib.metadata.distribution("py2app")
    out += [f"### py2app bootstrap {py2app.version}\n",
            f"- Licence: {_licence_id(py2app.metadata)}",
            f"- Upstream: {_url(py2app.metadata)}",
            "- Why it is here: the build tool; its bootstrap and launcher stub are "
            "embedded in the app\n"]
    for f in py2app.files or []:
        if ".dist-info/" in str(f) and _licence_file(str(f)):
            _details(out, str(f).rsplit("/", 1)[-1],
                     Path(py2app.locate_file(f)).read_text(encoding="utf-8").rstrip())
    runtime = runtime_version(bundle)
    minor = ".".join(runtime.split(".")[:2])
    pylic = Path(sys.base_prefix) / "lib" / f"python{minor}" / "LICENSE.txt"
    out += [f"### Python {runtime}\n",
            "- Licence: Python Software Foundation License Version 2 (PSF-2.0)",
            "- Upstream: https://www.python.org/",
            "- Why it is here: py2app embeds the interpreter "
            "(`Contents/Frameworks/Python.framework`) and the standard library\n"]
    _details(out, "LICENSE.txt", pylic.read_text(encoding="utf-8").rstrip())

    out += ["## Native libraries\n",
            "The Python runtime links these; py2app copies them into",
            "`Contents/Frameworks/`. Versions are the Homebrew kegs they were copied",
            "from.\n"]
    groups: dict[str, list[str]] = {}
    for lib in native_libs(bundle):
        groups.setdefault(NATIVE[lib][0], []).append(lib)
    for component, libs in groups.items():
        _, formula, licence, sources = NATIVE[libs[0]]
        version = (BREW / formula).resolve().name
        out += [f"### {component} {version} ({', '.join(libs)})\n",
                f"- Licence: {licence}", f"- Homebrew formula: `{formula}`\n"]
        for source in sources:
            path, _, count = source.partition(":")
            text = (BREW / formula / path).read_text(encoding="utf-8").rstrip()
            if count:
                text = "\n".join(text.splitlines()[: int(count)])
            _details(out, path.rsplit("/", 1)[-1], text)

    out += ["## The panel\n",
            "`BobPanel`, the Swift window, is built with SwiftPM. Versions are the pins in",
            "`panel/Package.resolved`; the texts are from the checkouts under",
            "`panel/.build/checkouts/`.\n"]
    for pin in json.loads(RESOLVED.read_text(encoding="utf-8"))["pins"]:
        title, licence, why = SWIFT[pin["identity"]]
        suffix = " (resolved, not linked)" if "not linked" in why else ""
        out += [f"### {title} {pin['state']['version']}{suffix}\n", f"- Licence: {licence}",
                f"- Upstream: {pin['location'].removesuffix('.git')}",
                f"- Why it is here: {why}\n"]
        checkout = next(p for p in CHECKOUTS.iterdir() if p.name.lower() == title.lower())
        for lic in sorted(checkout.glob("LICENSE*")):
            _details(out, lic.name, lic.read_text(encoding="utf-8").rstrip())
    out += ["## The VS Code extension\n",
            "`vscode-extension/package.json` declares no runtime dependencies; the",
            "extension ships only its own code."]
    NOTICES.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {NOTICES.relative_to(REPO)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("what", choices=("manifest", "notices"))
    ap.add_argument("--bundle", type=Path, default=BUNDLE)
    ap.add_argument("--check", action="store_true", help="manifest only: compare, write nothing")
    args = ap.parse_args(argv)
    if not (args.bundle / "Contents").is_dir():
        sys.exit(f"frozen_components: no built app at {args.bundle}")
    if args.what == "manifest":
        return write_manifest(args.bundle, args.check)
    return write_notices(args.bundle)


if __name__ == "__main__":
    sys.exit(main())
