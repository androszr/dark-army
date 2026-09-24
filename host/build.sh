#!/bin/bash
# Build the Dark Army menu bar .app bundle, with the native panel bundled.
# Usage: cd host && ./build.sh [--install] [--dev] [--check-only]
#                              [--allow-untagged]
#
# Strict by default: the build REFUSES to produce an app unless the panel and
# the VS Code extension were both built successfully in this run and are at
# least as new as their sources, and unless this is a clean checkout on an
# exact vX.Y.Z tag (dark_army_menubar/build_check.py decides all three).
#   --install     copy the result to /Applications afterwards
#   --dev         bring back the old warn-and-continue fallbacks (no Swift
#                 toolchain, no npm, a stale panel, a committed .vsix), and
#                 build an unnumbered app. Never for a release.
#   --check-only  run the gates, then stop before py2app
#   --allow-untagged
#                 build an unnumbered app from an untagged or dirty tree, and
#                 nothing else: the panel and extension gates stay strict. This
#                 is what the in-app Rebuild verb passes, because a checkout
#                 mid-development is untagged every ordinary day and --dev
#                 would silently reinstate the fallbacks that let a stale panel
#                 ship.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# The bundle's name: `dist/$APP_NAME.app` and `/Applications/$APP_NAME.app`.
# `setup.py`'s `name=` and `CFBundleName` say the same; the bundle identifier
# (`com.bob-companion.menubar`) does not change with it.
APP_NAME="Dark Army"
APP="dist/$APP_NAME.app"
PANEL_DIR="$SCRIPT_DIR/../panel"
PANEL_BINARY="$PANEL_DIR/.build/release/BobPanel"
EXT_DIR="$SCRIPT_DIR/../vscode-extension"
BUILD_CHECK=(-m dark_army_menubar.build_check)

INSTALL=0
DEV=0
CHECK_ONLY=0
ALLOW_UNTAGGED=0
for arg in "$@"; do
    case "$arg" in
        --install) INSTALL=1 ;;
        --dev) DEV=1 ;;
        --check-only) CHECK_ONLY=1 ;;
        --allow-untagged) ALLOW_UNTAGGED=1 ;;
        *)
            echo "usage: ./build.sh [--install] [--dev] [--check-only] [--allow-untagged]" >&2
            exit 2
            ;;
    esac
done

# A refusal names what is stale and how to build anyway; the footer is the
# one place the escape hatch is spelled out, and it says what it is for.
# There are two escapes and they are not interchangeable: --allow-untagged
# answers the version refusal alone and keeps every staleness gate strict,
# while --dev reinstates the warn-and-continue fallbacks and is never for a
# release. The version refusal is the one a developer meets on an ordinary
# day, so it must not be footed with the more dangerous of the two.
# DIE_HINT=none prints no footer at all, for a refusal no flag can answer.
die() {
    echo "==> REFUSED: $*" >&2
    if [ "${DIE_HINT:-}" != "none" ]; then
        echo "    (pass ${DIE_HINT:---dev} to build anyway — never for a release)" >&2
    fi
    exit 1
}

# Fail fast with an actionable message rather than a bare "No such file".
if [ ! -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    echo "error: host venv not found at host/.venv" >&2
    echo "Create it first:" >&2
    echo "  cd host && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt" >&2
    exit 1
fi

# Whole seconds, compared >= below: a package finishing inside the same second
# the script started still counts as built by this run.
BUILD_START="$(date +%s)"

# The release version, decided ONCE and here — above everything that writes
# inside the checkout. The portrait rsync below and the extension packager both
# dirty the work tree during a run, so a version judged any later would read
# its own build as uncommitted work and bake `vX.Y.Z-dirty` on a perfectly
# clean tagged release. Strict builds refuse an untagged or dirty tree; --dev
# passes --allow-untagged and marks itself unnumbered. --check-only reaches
# this too, which makes it the cheap pre-release rehearsal.
RELEASE_ARGS=(release --repo-root "$SCRIPT_DIR/..")
if [ "$DEV" = 1 ] || [ "$ALLOW_UNTAGGED" = 1 ]; then
    RELEASE_ARGS+=(--allow-untagged)
fi
# stderr is kept OFF the value `read` parses. Merged in, a stray interpreter
# warning becomes a garbage version string that is then baked into the app and
# handed to plutil as an argument — a build that lies rather than one that
# refuses.
# mktemp rather than a $$-predictable name: `2>` follows symlinks, and a
# predictable path in a shared /tmp is a file another local process can aim.
VERSION_ERR="$(mktemp "${TMPDIR:-/tmp}/dark-army-version.XXXXXX")"
# PYTHONPATH rather than a cwd: `-m` resolves from the working directory, and
# this gate is the one build_check call that runs above the `cd "$SCRIPT_DIR"`
# below — it has to, because it must judge the tree before anything writes in
# it. Without this, `bash host/build.sh` from the repo root cannot import it.
if VERSION_LINE="$(PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
        "$SCRIPT_DIR/.venv/bin/python" \
        "${BUILD_CHECK[@]}" "${RELEASE_ARGS[@]}" 2>"$VERSION_ERR")"; then
    rm -f "$VERSION_ERR"
else
    VERSION_WHY="$(cat "$VERSION_ERR" 2>/dev/null || true)"
    rm -f "$VERSION_ERR"
    # Only three of the five refusals are the ones --allow-untagged answers.
    # An inconsistent extension or phone version is refused whatever the flags,
    # so footing those with a flag that changes nothing sends the reader in a
    # circle. `die` falls back to --dev when DIE_HINT is unset, which is right
    # for the staleness refusals and wrong for these two, so they get no hint.
    case "$VERSION_WHY" in
        *"tag"*|*"work tree"*|*"git checkout"*)
            DIE_HINT="--allow-untagged" die "version: $VERSION_WHY" ;;
        *)
            DIE_HINT="none" die "version: $VERSION_WHY" ;;
    esac
fi
read -r RELEASE_VERSION PLIST_VERSION <<<"$VERSION_LINE"
# setup.py reads this rather than re-running git after the build dirtied things.
export BOB_BUILD_VERSION="$RELEASE_VERSION"
echo "==> Version: $RELEASE_VERSION (bundle $PLIST_VERSION)"

# The build a release zips up — strict, not --install — must not carry the
# builder's home folder, and a checkout inside it guarantees that it will: the
# unstripped panel binary names every source file by absolute path, and
# py2app records the venv's python in Info.plist. Refused here, before the
# long build, rather than by the scan before signing below; --check-only
# reaches this too, so the rehearsal says it. --install and --allow-untagged
# builds are for this machine and never zipped.
RELEASE_ARTIFACT=0
if [ "$DEV" = 0 ] && [ "$ALLOW_UNTAGGED" = 0 ] && [ "$INSTALL" = 0 ]; then
    RELEASE_ARTIFACT=1
    REPO_ABS="$(cd "$SCRIPT_DIR/.." && pwd -P)"
    HOME_ABS="$(cd "${HOME:-/}" 2>/dev/null && pwd -P || true)"
    if [ -n "$HOME_ABS" ] && [ "$HOME_ABS" != / ] \
            && [ "${REPO_ABS#"$HOME_ABS"/}" != "$REPO_ABS" ]; then
        DIE_HINT="--allow-untagged" die "release artifact: this checkout ($REPO_ABS) is inside your home folder, and the bundle would name it. Build the release from a checkout outside it, e.g. /Users/Shared/dark-army-release with its own host/.venv (the releasing skill, step 6)"
    fi
fi

# An app started at login inherits launchd's PATH, which is /usr/bin:/bin:
# /usr/sbin:/sbin — no Homebrew. `dev_build.rebuild()` runs this script with that
# environment, so a strict gate that trusted PATH alone would refuse
# Rebuild & Deploy on every login-launched app with node installed. Add the two
# standard prefixes when they are real, exactly as the daemon resolves its own
# PATH-installed tools. Above the panel step, so a toolchain reachable only
# through a prefix counts too. BOB_BUILD_PATH_DIRS overrides the list, which is
# how the probe is tested without a Homebrew on the test machine; an empty value
# means no probe.
for brew_bin in ${BOB_BUILD_PATH_DIRS-/opt/homebrew/bin /usr/local/bin}; do
    case ":$PATH:" in
        *":$brew_bin:"*) ;;
        *) [ -d "$brew_bin" ] && PATH="$PATH:$brew_bin" ;;
    esac
done
export PATH

# The panel bundles its own copy of the photo portraits (SwiftPM resources must
# live inside the target), so re-sync it from the source of truth before
# building — two copies of an asset set drift, and the one nobody regenerates is
# the one that ships. `assets/portraits` is the source of truth, the panel's
# `Resources/portraits` is a copy made here (and by `tools/portrait_ingest.py`),
# and it must land before signing.
if [ -d "$SCRIPT_DIR/../assets/portraits" ]; then
    rsync -a --delete "$SCRIPT_DIR/../assets/portraits/" \
        "$PANEL_DIR/Sources/BobPanel/Resources/portraits/" 2>/dev/null \
        || cp -R "$SCRIPT_DIR/../assets/portraits/." \
                 "$PANEL_DIR/Sources/BobPanel/Resources/portraits/"
fi

# The native panel. Swift's build is incremental, so this is cheap when nothing
# changed. Strict: a failed compile or a missing toolchain refuses the build;
# only --dev keeps the old warning, for a machine that wants a menu bar with no
# panel.
if command -v swift >/dev/null 2>&1 && [ -f "$PANEL_DIR/Package.swift" ]; then
    echo "==> Building the panel..."
    # Codex/VS Code launches can deny writes to ~/.cache even though the repo is
    # writable. Swift treats an unwritable Clang module cache as a manifest
    # failure, after which an old BobPanel binary left in .build would be the
    # one bundled — which the freshness gate below now catches. Keep both
    # caches in the writable temp area anyway.
    BOB_CLANG_CACHE="${TMPDIR:-/tmp}/dark-army-clang-cache"
    BOB_SWIFT_CACHE="${TMPDIR:-/tmp}/dark-army-swift-cache"
    mkdir -p "$BOB_CLANG_CACHE" "$BOB_SWIFT_CACHE"
    if ! ( cd "$PANEL_DIR" \
        && CLANG_MODULE_CACHE_PATH="$BOB_CLANG_CACHE" \
           SWIFTPM_MODULECACHE_OVERRIDE="$BOB_SWIFT_CACHE" \
           swift build -c release --disable-sandbox ); then
        if [ "$DEV" = 1 ]; then
            echo "==> WARNING: panel build failed — the Agents view will be unavailable" >&2
        else
            die "panel build failed"
        fi
    fi
    # A manifest edit that changes nothing SwiftPM compiles (22 Sep 2026: a
    # rename inside Package.swift) leaves the old binary in place and the
    # gate below refuses it as stale. Set that binary aside and relink once;
    # if the relink writes nothing, the old one goes back and the gate judges
    # it exactly as before.
    if [ -f "$PANEL_BINARY" ] && ! ( cd "$SCRIPT_DIR" && .venv/bin/python "${BUILD_CHECK[@]}" \
            panel --repo-root "$SCRIPT_DIR/.." >/dev/null 2>&1 ); then
        echo "==> The panel binary is older than its sources; relinking it"
        mv -f "$PANEL_BINARY" "$PANEL_BINARY.stale"
        ( cd "$PANEL_DIR" \
            && CLANG_MODULE_CACHE_PATH="$BOB_CLANG_CACHE" \
               SWIFTPM_MODULECACHE_OVERRIDE="$BOB_SWIFT_CACHE" \
               swift build -c release --disable-sandbox ) || true
        if [ -f "$PANEL_BINARY" ]; then
            rm -f "$PANEL_BINARY.stale"
        else
            mv -f "$PANEL_BINARY.stale" "$PANEL_BINARY"
        fi
    fi
elif [ "$DEV" = 1 ]; then
    echo "==> WARNING: no Swift toolchain — skipping the panel" >&2
else
    die "no Swift toolchain (install Xcode)"
fi
cd "$SCRIPT_DIR"

# A compile that "succeeded" can still leave an old binary behind (a change
# SwiftPM does not track, a manifest failure it swallowed). Refuse a binary
# older than any linked source, or a resource bundle that does not carry every
# file under Sources/BobPanel/Resources.
if [ "$DEV" != 1 ] || [ -e "$PANEL_BINARY" ]; then
    if ! PANEL_CHECK="$("$SCRIPT_DIR/.venv/bin/python" "${BUILD_CHECK[@]}" panel \
            --repo-root "$SCRIPT_DIR/.." 2>&1 >/dev/null)"; then
        if [ "$DEV" = 1 ]; then
            echo "==> WARNING: panel is stale: $PANEL_CHECK" >&2
        else
            die "panel is stale: $PANEL_CHECK"
        fi
    fi
fi

# Build the VS Code extension .vsix. Strict: node and npm must be present, the
# pipeline must succeed, and the package that ships must be the one this run
# wrote, named for — and carrying inside — the version package.json declares.
# --dev attempts the pipeline only when npm is present and otherwise takes the
# committed .vsix, but only one whose version matches the manifest. Without
# one, everything works except focusing the terminal *tab* (the window still
# raises).
build_extension() {
    # The path goes in as argv, not spliced into the JS source: a checkout path
    # containing a quote or backslash would otherwise be a SyntaxError, and under
    # `set -e` that aborts the whole .app build.
    EXT_VER="$(node -e 'console.log(require(process.argv[1]).version)' \
        "$EXT_DIR/package.json" 2>/dev/null || true)"
    [ -n "$EXT_VER" ] || return 1
    # Before the install, not after: npm refuses the same disagreement with an
    # EUSAGE wall that says nothing about which two files drifted apart.
    LOCK_CHECK="$("$SCRIPT_DIR/.venv/bin/python" "${BUILD_CHECK[@]}" lock \
        --ext-dir "$EXT_DIR" 2>&1 >/dev/null)" \
        || { echo "==> extension: $LOCK_CHECK" >&2; return 1; }
    echo "==> Building VS Code extension..."
    # The install is exactly the committed lockfile, and the packager comes out
    # of that same install — never fetched fresh off the network, which is how
    # two builds a week apart came to ship different bytes.
    ( cd "$EXT_DIR" \
        && VSCE_BIN=./node_modules/.bin/vsce \
        && npm ci --silent \
        && npm run build \
        && { [ -x "$VSCE_BIN" ] || {
                 echo "extension: vsce is not in node_modules/.bin — is @vscode/vsce in devDependencies?" >&2
                 exit 1
             }; } \
        && "$VSCE_BIN" package -o "dark-army-ide-$EXT_VER.vsix" \
               --allow-missing-repository --skip-license )
}
VSIX=""
# What the finished bundle's provenance note will say it was built from.
EXT_SOURCE="none"
if [ "$DEV" = 1 ]; then BUILD_MODE="dev"; else BUILD_MODE="strict"; fi
if [ "$DEV" = 1 ]; then
    if command -v npm >/dev/null 2>&1 && [ -f "$EXT_DIR/package.json" ]; then
        if build_extension; then
            EXT_SOURCE="built"
        else
            EXT_SOURCE="committed"
            echo "==> WARNING: extension build failed — using the committed .vsix" >&2
        fi
    else
        EXT_SOURCE="committed"
    fi
    VSIX="$("$SCRIPT_DIR/.venv/bin/python" "${BUILD_CHECK[@]}" vsix --ext-dir "$EXT_DIR")" \
        || VSIX=""
    if [ -z "$VSIX" ]; then
        EXT_SOURCE="none"
        echo "==> WARNING: no vscode-extension .vsix found — 'Reveal in VS Code' terminal-tab focus will be unavailable" >&2
    fi
else
    command -v node >/dev/null 2>&1 || die "extension: node is not on PATH"
    command -v npm >/dev/null 2>&1 || die "extension: npm is not on PATH"
    [ -f "$EXT_DIR/package.json" ] || die "extension: $EXT_DIR/package.json is missing"
    build_extension || die "extension build failed"
    VSIX="$("$SCRIPT_DIR/.venv/bin/python" "${BUILD_CHECK[@]}" vsix --ext-dir "$EXT_DIR" \
            --built-since "$BUILD_START" 2>&1)" \
        || die "extension: $VSIX"
    EXT_SOURCE="built"
fi
cd "$SCRIPT_DIR"

if [ "$CHECK_ONLY" = 1 ]; then
    echo "==> Checks passed (panel fresh, extension $(basename "${VSIX:-none}"))"
    exit 0
fi

# py2app is a dev dependency; check for it only now that the gates have run,
# so --check-only works on a venv without it.
if ! "$SCRIPT_DIR/.venv/bin/python" -c "import py2app" 2>/dev/null; then
    echo "error: py2app is not installed in host/.venv" >&2
    echo "Install the dev dependencies:" >&2
    echo "  cd host && .venv/bin/pip install -r requirements-dev.txt" >&2
    exit 1
fi

# Build .app with py2app
echo "==> Building .app bundle..."
cd "$SCRIPT_DIR"
rm -rf build dist
# Full output on purpose — truncating this hid py2app failures behind three
# meaningless trailing lines.
.venv/bin/python setup.py py2app

# Recompile the bundle's Python bytecode before anything is signed. py2app
# copies each package's source-tree `__pycache__` as it finds it, so
# `_version_info.py` (stamped by setup.py after its cache was made) shipped
# beside a cache naming an older version; and the `cp -R` install drops
# mtimes, so every timestamp cache is out of date in /Applications. CPython
# rewrites an out-of-date cache in place, and one written inside the signed
# bundle breaks its seal. An `unchecked-hash` cache (PEP 552) is loaded
# without comparing it to the source and is never rewritten; `-s` strips the
# builder's folder from each recorded file name. Runs in every mode.
echo "==> Recompiling the bundle's Python bytecode..."
PY_TAG="$("$SCRIPT_DIR/.venv/bin/python" -c 'import sys; print("python%d.%d" % sys.version_info[:2])')"
BUNDLE_LIB="$SCRIPT_DIR/$APP/Contents/Resources/lib/$PY_TAG"
[[ -d "$BUNDLE_LIB" ]] \
    || DIE_HINT="none" die "the bundle has no $PY_TAG library folder to recompile"
find "$BUNDLE_LIB" -type d -name __pycache__ -prune -exec rm -rf {} +
"$SCRIPT_DIR/.venv/bin/python" -m compileall -q -f \
    --invalidation-mode unchecked-hash -s "$SCRIPT_DIR/dist/" "$BUNDLE_LIB" \
    || DIE_HINT="none" die "could not recompile the bundle's Python bytecode"

# Bundle the panel binary — and its resource bundle, which is not optional.
# (The WARNING branches below are reachable only under --dev; the strict gates
# above have already refused a missing panel or .vsix.)
#
# SwiftPM generates `Bundle.module` with two candidates: a bundle beside the
# executable, and a **hardcoded absolute path into this checkout's .build
# directory**. Only the binary used to be copied, so the second candidate is the
# one that answered: every installed app has been loading the cast out of the
# developer's source tree, and would `fatalError` on the first avatar the moment
# that tree moved or was cleaned. It looked like it worked because the path
# happened to exist on the machine that built it.
#
# **The panel ships as a nested .app, not as a bare executable.** It used to sit
# at `Contents/Resources/BobPanel`, and CFBundle only walks up from
# `Contents/MacOS` — so the running panel had no bundle identity at all. Inside
# the process that was the documented reason notifications live in the menu bar
# (`Bundle.main.bundleIdentifier` was nil); outside it was worse, and measured:
# every other app on the machine saw `NSRunningApplication.bundleIdentifier ==
# nil` for the frontmost window on screen. A dictation tool that asks the system
# which app is in front, and keys anything on its identifier, gets nothing back
# and reports no active text field — which is exactly what MacWhisper logged
# against every text box in this panel while TextEdit worked in the same second.
# An Info.plist welded into the executable's __TEXT,__info_plist section fixes
# the *inside* half only; LaunchServices reads identity from the bundle
# directory, so the directory has to exist.
# The Dock labels this tile from the bundle's folder name, ignoring
# CFBundleDisplayName. The folder is therefore Dark Army.app. It stays nested,
# with its own bundle id, so it is not a second copy of the menu-bar app.
PANEL_APP="$APP/Contents/Resources/Dark Army.app"
# What the manifest may claim about the nested bundle's version keys. Empty
# unless the stamp below actually ran: a --dev build with no panel binary
# produces no BobPanel.app, and a manifest asserting a write that did not
# happen is the class of untruth this whole change exists to end.
PANEL_STAMPED=""
if [ -x "$PANEL_BINARY" ]; then
    echo "==> Bundling the panel..."
    rm -rf "$PANEL_APP"
    mkdir -p "$PANEL_APP/Contents/MacOS" "$PANEL_APP/Contents/Resources"
    cp "$PANEL_DIR/Sources/BobPanel/Info.plist" "$PANEL_APP/Contents/Info.plist"
    # The nested bundle carries the same number as the app around it. The copy
    # welded into the executable's __TEXT,__info_plist by Package.swift still
    # says 1.0/1: rewriting that source plist would dirty the very tree the
    # release gate just judged, and nothing in panel/Sources reads either key.
    # The divergence is recorded honestly as `panel_embedded` in the release
    # manifest written below rather than hidden.
    plutil -replace CFBundleShortVersionString -string "$PLIST_VERSION" \
        "$PANEL_APP/Contents/Info.plist"
    plutil -replace CFBundleVersion -string "$PLIST_VERSION" \
        "$PANEL_APP/Contents/Info.plist"
    PANEL_STAMPED="$PLIST_VERSION"
    # The Dock names a directly launched binary after the file. Dark Army is
    # that name; the Swift product stays BobPanel.
    cp "$PANEL_BINARY" "$PANEL_APP/Contents/MacOS/Dark Army"
    plutil -replace CFBundleExecutable -string "Dark Army" \
        "$PANEL_APP/Contents/Info.plist"
    # Same icns as the menu-bar bundle. The panel is the process that appears
    # in the Dock (the menu bar is LSUIElement); without this the tile is a
    # generic executable even though Finder already shows the right face.
    if [ -f "$SCRIPT_DIR/AppIcon.icns" ]; then
        cp "$SCRIPT_DIR/AppIcon.icns" "$PANEL_APP/Contents/Resources/AppIcon.icns"
    fi
    # Kept for one release: an installed copy is replaced wholesale, but a
    # menu bar left running from before this change still looks here.
    cp "$PANEL_BINARY" "$APP/Contents/Resources/BobPanel"
    PANEL_BUNDLE="$(dirname "$PANEL_BINARY")/BobPanel_BobPanel.bundle"
    if [ -d "$PANEL_BUNDLE" ]; then
        rm -rf "$APP/Contents/Resources/BobPanel_BobPanel.bundle"
        cp -R "$PANEL_BUNDLE" "$APP/Contents/Resources/"
        cp -R "$PANEL_BUNDLE" "$PANEL_APP/Contents/Resources/"
    else
        echo "==> WARNING: no panel resource bundle — the panel will fall back to" >&2
        echo "    this checkout's .build directory and break if it moves" >&2
    fi
else
    echo "==> WARNING: no panel binary to bundle" >&2
fi

# Bundle the VS Code extension .vsix (menu bar app installs it on first run).
if [ -n "${VSIX:-}" ] && [ -f "$VSIX" ]; then
    echo "==> Bundling VS Code extension ($(basename "$VSIX"))..."
    cp "$VSIX" "$APP/Contents/Resources/"
else
    echo "==> WARNING: no vscode-extension .vsix found — 'Reveal in VS Code' terminal-tab focus will be unavailable" >&2
fi

# Bundle the /ship close-out script; the menu bar app installs it to
# ~/.dark-army/dark-army-close-out on every launch (hooks.py).
cp "$SCRIPT_DIR/../.claude/skills/ship/close-out.sh" \
   "$APP/Contents/Resources/dark-army-close-out.sh"

# The credits and the licence travel with the app (THIRD_PARTY_NOTICES.md names
# every outside component the bundle carries). Copied before the manifests and
# the signing below — anything added to Resources afterwards breaks the seal.
cp "$SCRIPT_DIR/../THIRD_PARTY_NOTICES.md" "$APP/Contents/Resources/"
cp "$SCRIPT_DIR/../LICENSE" "$APP/Contents/Resources/"

# The short list of what went into this build: numbers only, no paths, no
# hostname, no timestamp — it ships in a public zip. --allow-untagged here is
# not a second chance at the gate; the gate above already decided, and
# --version carries its answer so a tree the build has since dirtied is never
# re-judged.
"$SCRIPT_DIR/.venv/bin/python" "${BUILD_CHECK[@]}" release \
    --repo-root "$SCRIPT_DIR/.." --allow-untagged \
    --version "$RELEASE_VERSION" --panel-version "$PANEL_STAMPED" \
    --manifest "$APP/Contents/Resources/release-manifest.json"

# Stamp the checkout this bundle came from, so the installed copy can find its
# own source again (host/dark_army_menubar/dev_build.py reads it back and
# verifies it still holds a host/build.sh). Without it the app in /Applications
# has no repo ancestor, so the build row and the Rebuild verb disappeared for the
# one install everybody actually runs. Written before signing — anything added
# to Resources afterwards breaks the seal.
# **Only under --install.** The stamp is a developer convenience for the copy
# in /Applications; a bundle zipped up and handed to somebody else must not
# carry the builder's home directory in readable text, and a rebuild button
# pointing at a folder that does not exist on their machine is worse than no
# button. Without the stamp `find_repo_root()` returns None away from the
# checkout, `can_rebuild` goes false and the Rebuild row disappears by itself.
# A plain `./build.sh` bundle still finds its source through the ancestor walk,
# because host/dist/ is inside the repo.
if [ "$INSTALL" = 1 ]; then
    ( cd "$SCRIPT_DIR/.." && pwd ) > "$APP/Contents/Resources/repo-root"
fi

# The provenance note: what this app was actually built from — the extension
# version, the tool versions this machine had, and a fingerprint of the
# lockfile the extension was installed from. Non-fatal on purpose: a missing
# note has no correctness consequence and must not refuse an otherwise good
# build. Written here, before signing, for the same reason the stamp above is —
# anything added to Resources afterwards breaks the seal.
"$SCRIPT_DIR/.venv/bin/python" "${BUILD_CHECK[@]}" manifest \
    --repo-root "$SCRIPT_DIR/.." --ext-dir "$EXT_DIR" \
    --out "$APP/Contents/Resources/build-manifest.json" \
    --vsix "${VSIX:-}" --mode "$BUILD_MODE" --extension-source "$EXT_SOURCE" \
    >/dev/null || echo "==> WARNING: build manifest not written" >&2

# The scan behind the checkout test above: every file in the finished bundle,
# inside python312.zip included, searched for this machine's home folder.
# Only for the release artifact; a hit refuses before anything is signed.
if [ "$RELEASE_ARTIFACT" = 1 ]; then
    echo "==> Scanning the bundle for your home folder..."
    if ! HOME_WHY="$("$SCRIPT_DIR/.venv/bin/python" "${BUILD_CHECK[@]}" home \
            --app "$APP" 2>&1)"; then
        DIE_HINT="none" die "release artifact: ${HOME_WHY#build-check: }"
    fi
fi

# Re-sign, because the two copies above broke the seal py2app signed. A bundle
# whose sealed resources do not match its signature still launches — nothing
# anywhere says it is broken — but it is broken, and `codesign --verify` said so
# on every build we ever shipped.
#
# Ad-hoc (`-s -`) is enough for this and for the notification authorization
# prompt; no Developer ID is involved. It is not enough for macOS privacy
# answers (Documents, Photos, Music…): an ad-hoc app's designated requirement
# is its cdhash, which every build changes, so every build asks again.
# `DARK_ARMY_SIGN_ID` names a stable signing identity (an Apple Development
# certificate is enough) whose requirement survives rebuilds; unset, the build
# stays ad hoc. The panel binary is signed first, since signing the app seals
# resources by hash but does not sign a nested Mach-O for you.
# Launchpad looks up `AppIcon.icns.icns` when the key includes the extension
# and then draws the generic tile. The panel's key is already `AppIcon`.
plutil -replace CFBundleIconFile -string AppIcon "$APP/Contents/Info.plist"

SIGN_ID="${DARK_ARMY_SIGN_ID:--}"
echo "==> Signing..."
echo "    identity: ${SIGN_ID}"
if [ -d "$PANEL_APP" ]; then
    codesign --force --sign "$SIGN_ID" "$PANEL_APP/Contents/MacOS/Dark Army"
    codesign --force --sign "$SIGN_ID" "$PANEL_APP"
fi
if [ -x "$APP/Contents/Resources/BobPanel" ]; then
    codesign --force --sign "$SIGN_ID" "$APP/Contents/Resources/BobPanel"
fi
codesign --force --sign "$SIGN_ID" "$APP"
# Verification is fatal on purpose: shipping a broken seal is how the banners
# went quiet for a day without anyone suspecting the build.
codesign --verify --strict "$APP"

# Take the build output back out of LaunchServices.
#
# py2app leaves `dist/Dark Army.app` on disk and macOS registers it, so the
# machine ends up with *two* apps claiming `com.bob-companion.menubar`. The one
# that suffers is NotificationCenter: it resolves the bundle id to a copy that is
# not the process posting, and rather than fail it renders the app name over the
# localized word "Notification" — every banner arrived stripped of the title,
# subtitle and body we posted, with `notifier.py` logging the full text it had
# just handed over. Nothing else on the machine misbehaves, which is why this
# looked for a whole day like a bug in the notification code.
#
# Unregistered on every build, not only on `--install`: a checkout that builds
# without installing has the same duplicate. The install below re-registers
# /Applications, which is the copy that should own the identifier.
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [ -x "$LSREGISTER" ]; then
    "$LSREGISTER" -u "$PWD/$APP" >/dev/null 2>&1 || true
fi

echo "==> Built: $APP"

# Install if requested
if [ "$INSTALL" = 1 ]; then
    echo "==> Installing to /Applications..."
    rm -rf "/Applications/$APP_NAME.app"
    cp -R "$APP" "/Applications/$APP_NAME.app"
    if [ -x "$LSREGISTER" ]; then
        # Register the installed copy explicitly and unregister the build output
        # again — the copy above can put dist back in the database.
        "$LSREGISTER" -f "/Applications/$APP_NAME.app" >/dev/null 2>&1 || true
        "$LSREGISTER" -u "$PWD/$APP" >/dev/null 2>&1 || true
    fi
    echo "==> Installed to /Applications/$APP_NAME.app"
    # The copy on disk changed; the process did not. A daemon started from
    # the previous bundle keeps running the previous code until it is
    # relaunched, and nothing else says so — the menu bar's Rebuild & Deploy
    # restarts itself, a manual --install does not. (Found the hard way: a
    # rung added to daemon.py sat installed for hours while the old process
    # kept raising the card it was written to withhold.)
    if pgrep -f "/Applications/$APP_NAME.app/Contents/MacOS/$APP_NAME" >/dev/null 2>&1; then
        echo "==> $APP_NAME is still running the previous build — restart it" >&2
        echo "    (menu bar → Restart) for the install to take effect" >&2
    fi
fi
