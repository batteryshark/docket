# Optional Docket panel — source only

The panel lists outstanding items and undeliverable history and can request
removal. It does not deliver wakes or implement a delivery callback. A consumed
wake cannot be undone; removal feedback must not claim otherwise.

Manifest: extension 0.1.0, SDK 2.3.2, OpenChamber >=2.3.0. This range is not
fresh compatibility proof. Requires Node/npm plus separately approved SDK and
esbuild dependencies. After approval, `npm install` and `npm run build` in this
folder would produce `panel/main.js`. No dependencies, bundle or icon are
included here; the manifest's `icon.svg` has not been supplied. The root comic
marks do not fill that manifest entry. **Do not activate this incomplete extension.**

After build/asset/license/security review, the operator would add this folder
in Settings → Extensions and consent to its host service. The relay reads the
core's private token server-side and keeps it out of the iframe, discovers the
core through its state/loopback override, and exposes bounded list/history/
remove routes. This is separate from daemon own-client pairing. A working
panel does not prove busy-turn delivery or revocation.

Browser/CDP tests and full-suite discovery remain stopped: the prior browser
updater escaped isolation. Sources are retained for later containment review;
do not run `test/browser.mjs`, panel tests or e2e to obtain a green count.
No extension install, service activation or genuine capture was performed here.
