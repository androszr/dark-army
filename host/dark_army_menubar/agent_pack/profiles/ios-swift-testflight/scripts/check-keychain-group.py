#!/usr/bin/env python3
"""Refuse a keychain access group referenced by some config files and not others.

The group an app shares with its extensions is spelled once, in the build
settings, and every file that needs it references `$(KEYCHAIN_ACCESS_GROUP)`
rather than the literal. A literal inlined in one file drifts the day the team
prefix or the bundle id changes; a reference in the app's entitlements and not
in the Info.plist the code reads it from means the app writes to one group and
reads from another. Nothing says so: the widget simply renders "sign in" beside
a signed-in app.

So the rule is all or nothing. No file referencing the group is consistent —
that is the state of this app today, which declares no keychain group. The
moment one file references it, every file must, exactly once, and the guard
names each file that does not.

Counted as plain text rather than parsed, so the check is the same whether the
reference sits in `keychain-access-groups` or a custom Info key. Pass file paths
as arguments to run the guard against doctored copies — that is how its bite is
tested.
"""

import sys

DEFAULT_FILES = (
    'ios/Config/{{APP}}.entitlements',
    'ios/Config/{{APP}}-Release.entitlements',
    'ios/Config/Info.plist',
    'ios/Config/Info-Debug.plist',
)
TOKEN = '$(KEYCHAIN_ACCESS_GROUP)'


def count(path: str) -> int:
    with open(path, encoding='utf-8') as handle:
        return handle.read().count(TOKEN)


def main(argv: list[str]) -> int:
    files = tuple(argv) if argv else DEFAULT_FILES
    counts = {path: count(path) for path in files}

    # No file referencing the group is consistent: the app declares none.
    if not any(counts.values()):
        return 0

    referencing = [path for path, n in counts.items() if n]
    offenders = [path for path, n in counts.items() if n != 1]
    for path in offenders:
        others = len([p for p in referencing if p != path])
        print(
            f'::error::keychain-group: {path} references {TOKEN} {counts[path]} times, '
            f'expected 1 (the group is referenced by {others} other file(s))'
        )
    return 1 if offenders else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
