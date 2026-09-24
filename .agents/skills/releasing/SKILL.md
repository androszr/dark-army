---
name: releasing
description: Cuts a new versioned release of the Dark Army macOS menu bar app on GitHub. Use when the user asks to release, cut a release, publish a release, ship a version, or bump the version. There is no release-building CI, so the .app is built locally with host/build.sh and uploaded to a GitHub release.
---

# Releasing Dark Army

The remote is **GitHub** (`origin` → `github.com/androszr/bob-companion`), and
the default branch is **`main`**. GitLab is not used and its remote has been
removed. There is **no release-building CI**, so the release artifact is
**built locally** with `host/build.sh` and attached to a GitHub release.

**The manual steps are: merge to `main`, tag `vX.Y.Z`, build the app, upload it to
a GitHub release.**

> If release-building CI is added later, revise this skill to push the tag and let
> CI build and upload.

## How versioning works

**The git tag is the only source of truth, and the build now checks it.** There
is still no version string to hand-edit anywhere; `host/build.sh` asks
`build_check release` for the number *before* it does anything else, and:

- HEAD on a clean `vX.Y.Z` tag → the version is `vX.Y.Z`.
- Anything else → **the strict build REFUSES** and names which of the three it
  is: not a git checkout, HEAD is not on an exact `vX.Y.Z` tag, or the work tree
  has uncommitted changes (it lists the first few paths).
- Two more refusals apply to *every* build, `--dev` included, because they are
  about internal consistency rather than about having a number: the extension
  manifest's version must be `X.Y.Z`, and the phone project must declare a
  single `MARKETING_VERSION`.
- `--dev` downgrades the first three to a note and builds an **unnumbered**
  app. `--dev` is **never** a release.
- `--allow-untagged` does *only* that downgrade, leaving the panel and
  extension gates strict. It exists for the in-app Rebuild verb, which runs on
  a checkout that is untagged and dirty every ordinary day. It is also never a
  release, but it is the safer of the two for everyday use.

So **"bump the version" = create the git tag**, and you must **tag before
building**.

**What the number lands in.** The descriptive string (`vX.Y.Z`, or
`<branch>+<N>@<sha>[-dirty]` under `--dev`) is baked into `_version_info.py`
and is what the app's own `⋯` menu shows. The operating system's fields —
`CFBundleVersion` and `CFBundleShortVersionString`, on the app **and** on the
nested `BobPanel.app` — take the plain `X.Y.Z` derived from it. Those fields are
dot-separated digits, so an unnumbered build says **`0.0.0`** rather than
borrowing a number it does not have. Both keys take the same string: there is no
App Store submission and no Sparkle feed behind this app, so a separate
monotonic build counter would be provenance we do not have.

**Residuals, written down rather than hidden.**

- The `Info.plist` welded into the panel executable's `__TEXT,__info_plist` by
  `panel/Package.swift` still says `1.0`/`1`. Rewriting that source file before
  `swift build` would dirty the very tree the gate just judged, and nothing in
  `panel/Sources` reads either key — so the divergence is invisible in-process
  and visible only to a tool that dumps the section. It is recorded as
  `panel_embedded` in `release-manifest.json`.
- Both copies of `BobPanel` still carry the SwiftPM `Bundle.module` fallback
  path into this checkout's `.build` directory. `build.sh` defeats it by copying
  the resource bundle next to the executable so the *first* candidate answers.
  The only real fix moves `.build`, which is hardcoded in four places; not
  attempted.

Tags are semver `vX.Y.Z`. Decide patch vs minor by what landed since the last tag —
bugfixes only → patch; new user-facing features → minor. Confirm with the user if
unsure:

```bash
git log "$(git describe --tags --abbrev=0)"..main --oneline
```

## Prerequisites

- `gh` (GitHub CLI) authenticated: `brew install gh && gh auth login`.
  Without it, use the **web UI** fallback in step 6.
- A clean working tree **and a `vX.Y.Z` tag on HEAD** — the build refuses without both.
- **A checkout outside your home folder**, e.g. `/Users/Shared/dark-army-release`,
  with its own `host/.venv`. The release build refuses a checkout under `$HOME`:
  the panel binary names every Swift source by absolute path, and py2app writes
  the venv's python into `Info.plist`, so a home checkout puts your home folder
  in the zip. It also scans the finished bundle for it before signing.
- A **Swift toolchain** (Xcode) and **node/npm**, because `build.sh` builds the
  native panel and packages the editor extension. You do not have to eyeball the
  output for either: the build **refuses** a missing or stale panel and an
  extension package that was not built in this run or does not carry the
  manifest's version. `--dev` reinstates the old warn-and-continue fallbacks and
  is **never** used for a release.

## Release workflow

```
- [ ] 1. Merge the feature branch to main
- [ ] 2. Sync local main
- [ ] 3. Confirm the version number (patch vs minor)
- [ ] 4. Verify the tree is clean
- [ ] 5. Tag vX.Y.Z on main HEAD and push the tag
- [ ] 6. Build the .app locally, zip it, write the release notes, create the GitHub release with the asset
- [ ] 7. Verify the release + asset
```

**1–2. Merge and sync** (via a pull request, or fast-forward if you own the branch):
```bash
# after the PR is merged on GitHub:
git checkout main && git pull --ff-only
```

**4. Clean-tree check.** `_version_info.py`, `host/dist/`, `host/build/` and
`panel/.build/` are gitignored, so they don't dirty the tree:
```bash
git status --porcelain   # must print nothing
```

**5. Tag and push** — tag on `main` HEAD, then push it:
```bash
git tag -a vX.Y.Z -m "Dark Army vX.Y.Z — <one-line summary>"
git push origin vX.Y.Z
```

**6. Build locally and publish the release.** Tagging first means `build.sh`
bakes the clean `vX.Y.Z` version. Build in the neutral checkout, never in the
one under your home folder (the build refuses that):
```bash
# once: git clone <this repo> /Users/Shared/dark-army-release
#       cd /Users/Shared/dark-army-release/host && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cd /Users/Shared/dark-army-release && git fetch --tags && git checkout vX.Y.Z
cd host && ./build.sh            # the strict build: panel + extension + py2app -> dist/Dark Army.app
                                # it prints "==> Version: vX.Y.Z (bundle X.Y.Z)" first
cd dist
ditto -c -k --keepParent "Dark Army.app" dark-army-macos-arm64.zip   # ditto preserves macOS metadata
cd ../..
```

**The release notes are written now, from the commits.** There is no running
list of changes to copy from: the notes are every commit since the previous
release tag, which in this repository are already whole sentences a person can
read. From the repository root:

```bash
# release-notes
tag=vX.Y.Z
prev=$(git describe --tags --abbrev=0 "$tag^" 2>/dev/null || true)
mkdir -p host/dist
{
  echo "## Highlights"; echo
  echo "<replace: three to six sentences a person cares about, picked from the list below>"; echo
  echo "## Every change"; echo
  git log --no-merges --format='- %s' ${prev:+"$prev"..}"$tag"
  if [ -n "$prev" ]; then echo; echo "Compare: https://github.com/androszr/bob-companion/compare/$prev...$tag"; fi
} > host/dist/release-notes.md
```

Then open `host/dist/release-notes.md` and replace the Highlights placeholder
by hand with the changes a person will notice, picked from the list under it.
The first release has no earlier tag, so its list is the whole history and it
carries no compare link; its Highlights say what Dark Army is and does rather
than what changed. Show the finished file to the person before publishing. It
is written **after** the build because `build.sh` empties `host/dist/` when it
starts, and it lives in `host/dist/` because that folder is git-ignored, so the
tagged tree stays clean.

```bash
gh release create vX.Y.Z host/dist/dark-army-macos-arm64.zip \
  --repo androszr/bob-companion \
  --title "vX.Y.Z — <theme>" \
  --notes-file host/dist/release-notes.md
```

The finished bundle carries `Contents/Resources/release-manifest.json` — the
record of what shipped with what: the app's number and commit, the panel's, the
extension's, and the phone's declarations (`panel` is blank when no nested
`BobPanel.app` was produced, which only a `--dev` build does). Numbers only; it contains no
filesystem path, no hostname and no timestamp, because it ships in the zip.

The bundle you zip up carries **no `repo-root` stamp**: `build.sh` writes that
only under `--install`, so its `⋯` menu shows no Rebuild row (the build line
reads *release (no source)*). The stamp was never the only path in it: the
panel binary, `Info.plist` and py2app's `site.pyc` carry the checkout's
absolute path, which is why the release is built outside your home folder and
why the strict build scans the finished bundle (`build_check home`) and refuses
before signing if your home folder appears anywhere, inside `python312.zip`
included. Your own machine still gets the stamp, from `./build.sh --install`.

**If the build refuses.** A `REFUSED` line names the component and stops before
anything is packaged, which is the point — nothing stale ships.

- *panel build failed* or *no Swift toolchain*: install/select Xcode
  (`xcode-select -p`) and rerun.
- *panel is stale*: `swift build` left an older binary than its sources.
  `rm -rf panel/.build` and rerun.
- *extension*: the package on disk does not carry the version in
  `vscode-extension/package.json`, or `node`/`npm` is missing. Fix the version
  or install node, then rerun.
- *extension lockfile*: `vscode-extension/package-lock.json` no longer
  describes the `package.json` beside it — a dependency was added, removed or
  re-ranged and the lockfile was not re-recorded. The refusal names which two
  entries disagree. Run `cd vscode-extension && npm install`, commit the
  lockfile, then rerun. Never "fix" it by deleting the lockfile: it is the pin
  the release installs from.
- *version*: HEAD is untagged, the tree is dirty, or this is not a checkout.
  Tag it, or commit what is outstanding — see step 4. This is the one refusal
  `--check-only` will show you on any ordinary working day, which is the point:
  `./build.sh --check-only` is the pre-release rehearsal. To build a
  development copy without reaching for `--dev`, pass `--allow-untagged`: it
  gets past this refusal and nothing else.

Never pass `--dev` to get past a refusal in a release; that is exactly the
silent-stale-ship this gate exists to prevent. `./build.sh --check-only` runs
the gates and stops before the slow steps if you just want to see them pass.

**Commit the `.vsix` before tagging, not after.** `vsce` output is not
byte-stable, so a release build repackages the same version to different bytes
and leaves the committed `.vsix` showing as modified. That is now a *refusal*
for the next release build, because the version gate will not accept a dirty
tree — so land it with the version bump, before step 5. Never `--dev` around it.

**Within one session, that bites twice, and the repair is one line.** Every
build that reaches the extension step — `--check-only` included, which is why
the rehearsal poisons the run it rehearses — repackages the `.vsix`. So after
*any* build, before the next one:

```bash
git checkout -- vscode-extension/*.vsix
```

Restore it rather than committing it: a commit on top of the tag moves HEAD off
the tag, and the version gate then refuses for the other reason. The same
applies to the "update your own machine" step below, which runs straight after
a release build has rewritten the file.

**Two manifests ship in `Contents/Resources`, and their rules differ on
purpose.** `release-manifest.json` is numbers only — no timestamps, no paths,
no hostname — because it is the record of *what shipped*. `build-manifest.json`
is the build's own provenance note and does carry `built_at` and the tool
versions. Neither carries a filesystem path. One is not a leak of the other.

**Web UI fallback (no `gh`):** GitHub → **Releases → Draft a new release** →
choose the existing `vX.Y.Z` tag → attach `dark-army-macos-arm64.zip` →
add the title, and paste `host/dist/release-notes.md` as the description →
**Publish release**.

**7. Verify:**
```bash
gh release view vX.Y.Z --repo androszr/bob-companion   # expect the .zip asset listed
```

## Optional: update your own machine

To run the released build locally (not part of publishing). The release build
you just ran repackaged the `.vsix`, so restore it first or this refuses for a
dirty tree:
```bash
git checkout -- vscode-extension/*.vsix
cd host && ./build.sh --install   # rebuilds at the tag, bakes the clean version, copies to /Applications
```
Then quit and relaunch the app so it picks up the new bundle.
