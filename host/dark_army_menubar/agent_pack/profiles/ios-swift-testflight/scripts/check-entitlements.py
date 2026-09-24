#!/usr/bin/env python3
"""Refuse drift between the Debug and Release entitlements.

The two files are identical. The one key they are ALLOWED to disagree on is
`aps-environment`, and it is optional: an app with no push capability carries
it in neither file, and that is consistent — it is the state of this app today.
Once either file claims it, both must, each with its own environment: a
distribution build must claim the PRODUCTION APNs environment or App Store
Connect rejects the upload.

Everything else matching is the whole point of the split: a capability added to
one and not the other ships a Release build quietly missing it. Nothing on the
device would say so: the widget simply stops seeing the keychain, or a passkey
ceremony simply fails.

Parsed rather than diffed, so a comment or a reordering is not a failure and a
real key change is. Pass the Debug and Release entitlements paths as arguments
(in that order) to run the guard against doctored copies — that is how its bite
is tested.
"""

import plistlib
import sys

DEBUG = 'ios/Config/{{APP}}.entitlements'
RELEASE = 'ios/Config/{{APP}}-Release.entitlements'
# The one key the two files are ALLOWED to disagree on, and the value each
# must carry. Sandbox for a cabled build, production for anything signed for
# distribution — they are different token universes and a token from one is
# `BadDeviceToken` at the other's host. Keyed by role, not by path, so a
# doctored pair passed on the command line gets the same rule.
APS = 'aps-environment'
EXPECTED = {'Debug': 'development', 'Release': 'production'}


def load(path: str) -> dict:
    with open(path, 'rb') as handle:
        return plistlib.load(handle)


def main(argv: list[str]) -> int:
    paths = tuple(argv) if argv else (DEBUG, RELEASE)
    if len(paths) != 2:
        print('::error::entitlements: pass exactly two entitlements paths, or none')
        return 1

    debug_path, release_path = paths
    debug = load(debug_path)
    release = load(release_path)

    errors = []

    # The APNs key is judged on its own below, with the file it is wrong in
    # named — reporting it here too would say the same slip twice.
    debug_keys = debug.keys() - {APS}
    release_keys = release.keys() - {APS}
    if debug_keys != release_keys:
        only_debug = sorted(debug_keys - release_keys)
        only_release = sorted(release_keys - debug_keys)
        errors.append(
            f'different keys — only in Debug: {only_debug}, only in Release: {only_release}'
        )

    for key in sorted(debug.keys() & release.keys()):
        if key == APS:
            continue
        if debug[key] != release[key]:
            errors.append(f'{key} differs: {debug[key]!r} (Debug) vs {release[key]!r} (Release)')

    # An app with no push capability carries the key in neither file, and
    # that is consistent. Once either file claims it, both must, each with
    # its own environment.
    if APS in debug or APS in release:
        for role, path, plist in (('Debug', debug_path, debug), ('Release', release_path, release)):
            actual = plist.get(APS)
            expected = EXPECTED[role]
            if actual != expected:
                errors.append(f'{path}: {APS} is {actual!r}, expected {expected!r}')

    for error in errors:
        print(f'::error::entitlements: {error}')
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
