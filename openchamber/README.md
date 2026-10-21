# Docket for OpenChamber

A rail panel that lists every outstanding Docket item, across projects and
agents, and the undeliverable history. The principal can remove a pending,
firing, admitted, or ambiguous item. If the wake was already consumed, the
panel says so and does not claim it was removed.

This panel is listing, history, and removal only. It does not deliver wakes.
It needs no delivery callback to the turn-owning host. It does not change the
OpenChamber app, the host, or settings. Historical Mac acceptance included visual
approval and actual installed relay health/snapshot/removal, including after a
principal-managed app update. This is not UI-click automation, current
source/platform certification or whole-goal completion.

The panel runs in OpenChamber's sandboxed iframe and cannot reach 127.0.0.1.
`service/main.mjs` is the host-owned relay. It discovers Docket from
`server.json` under `$DOCKET_HOME` or `~/.docket` (or from `DOCKET_URL` only
when that value is a loopback origin). It reads the Docket token on the
server side and never returns it to the iframe. The panel cannot choose a
URL, pass a reason or context into a request line, or proxy an arbitrary
HTTP call. OpenCode's port is not hardcoded; Docket discovers that itself.

Allowed relay routes:

- `GET /health` — ready for OpenChamber, does not call Docket
- `GET /snapshot` — outstanding items plus undeliverable history
- `POST /items/dkt_<16 hex>/remove` — cancel, or an honest refusal

Context is not sent to the iframe. Reasons are shown as text.

## Delivery is the daemon's job

Generic unpaired installs default to the official older OpenCode service
(`~/.local/state/opencode/service.json`). That host's queue behavior covers only
turns it owns; seeing a shared transcript is not proof of busy-turn ownership.
The historical Mac 0.3.0 deployment was paired after principal consent,
using Docket's own client against the current desktop origin with runtime port
rediscovery. The panel's server-side Docket-token relay is not this pairing or
a wake relay; no panel delivery callback needs adding.

Docket accepts arbitrary pairing labels and renames; the own token is persisted
before optional metadata checks. Each paired session request requires the saved
own-id active-record/revocation guard. Failure is visible, with no silent fallback
or anonymous retry. This guard is not a host-enforced capability boundary:
optional loopback auth can accept a missing/invalid bearer. Never borrow app or
other-client credentials. See [PAIRING.md](../PAIRING.md).

**Historical blocker only:** the earlier `desktopLocalClientToken` approach was
stopped when that key was absent. Supported own-client pairing supersedes it;
it is not a present delivery blocker. Historical acceptance included the actual
installed host route, not just a stand-in, but does not imply a fully green suite.

## Live service

This folder speaks the API in this checkout (outstanding states,
`delivery_state`, occurrence history, cancel of an admitted item). Historical
Mac 0.3.0 acceptance included CLI/MCP/HTTP consumed wakes and assistant ACKs,
with retained items through upgrade and Docket-service restart.
This panel does not restart anything or write settings. If a generic service is
older, the outstanding list may still load and the Undeliverable tab says history
needs the upgraded service.

Queue proof is natural assistant finish then new user/ACK in the same active
drain; urgent proof is at a model-step boundary, not mid-token generation. No
stronger continuing multi-step guarantee is claimed. Wake lateness text is
admission-time; final item lateness can include queue consumption wait. Removal
is best-effort across host-accept races: consumed, lost, missing, unreadable and
failed-retraction outcomes do not claim a witnessed removal.

No automatic app restart is authorized. A `file_stable` quiet-window check
cannot know future writes or semantic producer finality. Real revocation remains unproven;
do not destroy the final credential for a test. The whole goal is not declared done.
Use the root README for Docket stop/start/uninstall and safe backup/recovery;
never blindly restore a DB. Docket never executes item commands or contacts the
principal directly.

## Build

`panel/main.js` is already built. Rebuild only after changing `panel/main.ts`:

```
npm install
npm run build
```

OpenChamber does not compile the package. A folder install uses the files
here, including the built `panel/main.js`.

## Install

Choose this folder inside your own checkout for a future principal-managed
installation:

`<your-docket-checkout>/openchamber`

The principal adds that folder from Settings → Extensions → Add, and allows
the local service. That service only relays list and remove. Do not install
it from an agent session. Do not change OpenChamber settings from an agent
session. Installing this folder does not add a current-host delivery
callback.

After a `package.json` change, re-add the folder. After a panel change,
rebuild and reopen the panel.

## Verification safety

The historical browser-era full suite had two browser-startup failures
before panel code, not green. Browser/CDP testing and full discovery remain stopped after installed
Chrome's updater escaped isolation; no further browser-driven jobs until
containment is independently verified. Historical visual and installed relay/removal
acceptance does not certify current source/platform behavior.
