#!/usr/bin/env python3
"""Refuse a privacy usage string the app can reach and has not declared.

An in-process TCC gate with no usage description in the Info.plist does not
produce a denied permission — it terminates the process. There is no error
anywhere: the app simply vanishes while the user is typing. That is how a
dictation crash once shipped, and it is a whole class of bug rather than one bug.

So every gate this app can reach is declared in BOTH app plists — at minimum
the microphone and the speech recogniser, reached by the KEYBOARD's dictation
key: since iOS 16 it runs in the host process, so any free-text field in the
app is a reachable microphone, and iOS asks for both keys.

Debug is what a cabled build runs and Release is what TestFlight ships, so the
two must agree on every value: fixing Debug alone makes the crash vanish on the
developer's device while every tester still hits it.

The widget plists must carry NONE of them. A widget presents no keyboard and no
camera, so a usage string there is a claim the extension cannot justify — and
App Store review reads it.

Parsed rather than diffed, so a comment or a reordering is not a failure and a
real key change is. Pass the two app-plist paths as arguments to run the guard
against doctored copies — that is how its bite is tested.
"""

import plistlib
import sys

APP_PLISTS = ('ios/Config/Info.plist', 'ios/Config/Info-Debug.plist')
# Extension plists, if the app has any. Leave empty for an app with no extension.
WIDGET_PLISTS = ()

# Each key, and the API that reaches it. Add a line the moment the app can
# reach a new gate; remove one only when nothing can reach it any more.
# The microphone and speech pair is REQUIRED for any app with a free-text
# field: the keyboard's dictation key runs in-process since iOS 16.
REQUIRED = (
    'NSMicrophoneUsageDescription',  # keyboard dictation key, in-process since iOS 16
    'NSSpeechRecognitionUsageDescription',  # the same key's speech-to-text half
    # 'NSCameraUsageDescription',  # UIImagePickerController / AVCaptureDevice
    # 'NSPhotoLibraryUsageDescription',  # PHPickerViewController with write access
    # 'NSLocationWhenInUseUsageDescription',  # CLLocationManager
)


def load(path: str) -> dict:
    with open(path, 'rb') as handle:
        return plistlib.load(handle)


def main(argv: list[str]) -> int:
    app_plists = tuple(argv) if argv else APP_PLISTS
    if len(app_plists) != 2:
        print('::error::privacy-strings: pass exactly two app-plist paths, or none')
        return 1

    errors = []
    loaded = {path: load(path) for path in app_plists}

    for path, plist in loaded.items():
        for key in REQUIRED:
            if key not in plist:
                errors.append(f'{path}: {key} is missing — an undeclared TCC gate kills the process')
                continue
            value = plist[key]
            if not isinstance(value, str):
                errors.append(f'{path}: {key} is {type(value).__name__}, expected a string')
            elif not value.strip():
                errors.append(f'{path}: {key} is empty — iOS treats a blank string as no string')

    first, second = app_plists
    for key in REQUIRED:
        a, b = loaded[first].get(key), loaded[second].get(key)
        if a is not None and b is not None and a != b:
            errors.append(f'{key} differs: {a!r} ({first}) vs {b!r} ({second})')

    for path in WIDGET_PLISTS:
        widget = load(path)
        for key in REQUIRED:
            if key in widget:
                errors.append(
                    f'{path}: {key} does not belong in a widget — the extension presents no '
                    'keyboard and no camera, so the string is a claim it cannot justify'
                )

    for error in errors:
        print(f'::error::privacy-strings: {error}')
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
