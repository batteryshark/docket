# Optional Docket panel — source only

The panel lists outstanding items and undeliverable history and can request
removal. It does not deliver wakes or implement a delivery callback. A consumed
wake cannot be undone; removal feedback must not claim otherwise.

Manifest: extension 0.1.0, SDK 2.3.2, OpenChamber >=2.3.0. This range is not
verified compatibility. Building requires Node/npm, the SDK and esbuild;
`npm install` and `npm run build` in this folder would produce `panel/main.js`.
No dependencies, bundle or icon are included here; the manifest's `icon.svg`
has not been supplied. The root comic marks do not fill that manifest entry.
**Do not activate this incomplete extension.**

The relay reads the core's private token server-side and keeps it out of the
iframe, discovers the core through its state/loopback override, and exposes
bounded list/history/remove routes. This is separate from daemon own-client
pairing. A working panel does not prove busy-turn delivery or revocation.

## Known limitations

Do not run full test discovery, `test/browser.mjs`, panel browser/CDP tests or
e2e: a browser updater escaped an earlier isolation attempt. Sources remain for
containment review. Live host delivery, pairing, revocation and extension
activation remain unverified.
