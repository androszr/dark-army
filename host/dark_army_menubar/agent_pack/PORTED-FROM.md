# Ported from

Upstream repository: starter-pack
Commit: b114f221ba8d1adfa51dbb2a4cbe507d9531fc06

That is a private sibling repository, not published. Nothing at runtime
reads it, or any path outside the Dark Army bundle or this checkout.
This file is the only link back; the render is a port, not a dependency.

Re-port: copy `template/` and `profiles/` from that commit over this tree,
then re-run the agent-pack tests. Do not vendor `tools/` or `install.sh` —
their logic lives in `pack_render.py` and `pack_install.py`.

Local divergence a re-port must keep (24 Sep 2026): the `ios-swift-testflight`
profile is for a plain SwiftUI app. Upstream's `workflows/ios.yml` carries a
pnpm / `pnpm ios:gen` / generated-Swift job and `testflight.yml` a
`scripts/gen-swift-contracts.mjs` path from a web+iOS monorepo; both were
removed here. `check-entitlements.py`, the keychain-group checks and the APNs
read-back treat "no `aps-environment`, no keychain group" as consistent.
