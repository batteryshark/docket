# Optional own-client pairing — operator procedure, not performed here

Pair only after reviewing the host and obtaining explicit principal consent.
Docket becomes its own client with broad session API access, not wake-only
permissions. The returned bearer has no pairing-imposed expiry. No live
binding, Connection link, account identity or deployment proof is bundled.

In a private, non-recorded terminal, from the checkout run
`python3 -m docket pair --stdin`. It accepts a hidden prompt or secure stdin.
Never put a Connection link in argv, shell history, chat, reports, tool calls,
iframe content or a plaintext file. The principal creates a **This computer
only** Connection link in the host's device-enrollment UI, chooses any display
name, and privately enters it at the prompt within the enrollment window.
The UI/version workflow needs operator verification; no enrollment is automated.

The CLI identifies a numeric loopback host, then makes one redeem request,
without redirects/retries/credential borrowing. Names are display data, not
credential identity. Own token and client ID are saved atomically before
optional metadata checks. Missing/unknown optional metadata cannot select a
different origin or discard a minted bearer.

Private state is `$DOCKET_HOME/openchamber` (default `~/.docket/openchamber`):
0700 owned directory, 0600 own binding/identity/enrollment ledger files;
symlinks, hardlinks, foreign ownership and unsafe permissions are refused.
Enrollment digests are not a reusable link backup. Existing bindings are not
automatically replaced; concurrent enrollment is refused and a crashed lock
needs explicit inspection. After timeout, lost response, malformed result or
save failure, **do not retry the link**: the host may have minted a client.
Inspect/revoke through the operator UI and coordinate recovery instead.

Own enrollment state overrides generic/unpaired mode, including in new-code
daemons that notice enrollment later. Old running code needs reviewed upgrade;
pairing does not restart or install a service. Keep private consistent state
backups; no automatic reset/re-pair recovery is promised.

Before paired session requests, the adapter checks exactly one matching active
own client record with no expiry/revocation. Renaming is allowed. Failure is
visible without fallback. The guard is not a host-enforced capability boundary:
optional loopback auth and a guard/request race remain. **Live revocation is
unproven**; synthetic refusal tests are not proof. No live probes or pairing
are authorized by this staging. Admission/consumption/cancel and browser-safety
limitations in the root README still apply.
