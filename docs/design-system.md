# Signal design system

Signal is the visual system for the Mac panel and iPhone app. Its tracked
source is `design-system/tokens.json`. The ignored proposal in
`assets/proposals/2026-09-25-design-system/` records the visual direction;
the apps never read that proposal at runtime.

`tokens.json` has fixed `name: Signal`, `version: 1`, and exact `colors`,
`spacing`, `radii`, `type`, and `size` maps. Colors are six-digit hex. Dimensions
are whole points from 1 to 80, with phone controls and targets at least 44.
Text roles on their semantic surfaces must keep at least 4.5:1 contrast. The
generator and workshop import enforce these boundaries.

```bash
python3 tools/design_system_tokens.py
python3 tools/design_system_tokens.py --check
cd panel && swift build -c release
cd ../ios && xcodebuild -project BobPhone.xcodeproj -scheme BobPhone -sdk iphonesimulator build
```

The generator writes `SignalTokens.generated.swift` in both clients and
`tokens.json`, `tokens.css`, `tokens.js` in `design-system/workshop/`. It also
mirrors the six workshop files into each client's `Resources/workshop` folder.
Edit token values in the canonical JSON only; the generated files are outputs.
The iPhone widget extension compiles the phone's generated Swift file directly
from its app group in the Xcode project. `WidgetTheme` maps those shared colors
to compact WidgetKit roles; it owns no second palette or generated copy.
The Mac app's SwiftPM resources and the iPhone Xcode target bundle those
folders, so Settings → Advanced → *Open Signal workshop* and Menu → Design system work offline
without this checkout. The WebView can navigate only inside its workshop
folder and exposes no native bridge, network reader, or daemon action.

For component anatomy and layout, edit `design-system/workshop/index.html`,
`workshop.css`, or `workshop.js` for the web specimens; edit the corresponding
SwiftUI view and `Theme.swift` component for native behavior. The web
specimens are inspectable examples of Signal's tokens and component rules,
not a renderer reused by native screens. Run the generator to mirror web
files, then build both apps and compare the board, card, inbox, fleet, and
workshop at ordinary and large text sizes. Machine output and terminal grids
keep their monospace face.

The workshop's visible controls edit a preview draft. Import accepts only a
valid Signal version 1 JSON file. Copy export places validated JSON on the
clipboard when WebKit allows it and selects the same text for manual copy
otherwise. To promote an export, replace `design-system/tokens.json` with
that JSON, run the generator, run `--check`, then rebuild and distribute the
native clients. No workshop edit changes a running native client.
