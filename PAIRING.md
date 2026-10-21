# Docket own-client pairing

Historical Mac 0.3.0 acceptance included own-client pairing and delivery.
This procedure remains for future
explicitly authorized enrollment; generic installations work before pairing.
Never paste a Connection link into chat, a tool invocation, argv, a report, or an
iframe. Neither clipboard intake nor credential borrowing is implemented.
Historical acceptance included CLI/MCP/HTTP wake/ACKs, retained items through
Docket-service restart, and fresh own-client wake/ACK plus installed relay removal
after a principal-managed app update, without re-pairing. This does not certify
newer source/platform behavior or authorize another live test.

## Principal handoff — only after review and explicit pairing consent

**Disclosure:** Docket becomes its own named, revocable desktop client, with broad session API
access, not a wake-only capability. The returned token has no pairing-imposed expiry. Revoke
Docket in the same device list. Docket stores only its own returned token and binding, not
the app's desktop-local token, another client's token, or an OpenCode password.

1. From your Docket checkout, start this command in a private local terminal
   (Python 3.13 is the launcher default):

   ```sh
   bin/docket pair --stdin
   ```

   It waits at a **hidden** terminal prompt. No link is an argument or shell-history entry.
   A secure stdin pipe is also supported; never put the link in the pipe command itself or
   create a plaintext link file. Do not execute this through a transcript-recording tool.
2. Settings → Remote instances → Connect to this server → Add a device → choose **any display name** →
   **This computer only** → Create QR code → copy **Connection link** (not Create token).
   `Docket` is a suggested name only: `docket`, `DoCkEt`, or any other principal-chosen label
   is accepted. The reviewed 2.3.3 enrollment host gives the principal's name precedence over the
   redeem request's `clientLabel`/`deviceName` defaults. Names are not credential identity.
3. Paste at that hidden prompt and press Enter within ten minutes. The CLI performs an
   unauthenticated identifying `/health` GET, then exactly one redeem POST. No redirect,
   retry, multiple-host race or non-loopback candidate is allowed. IPv4/IPv6 numeric
   loopback only; `localhost` is deliberately rejected to avoid name-resolution ambiguity.
4. Enrollment does not restart or install anything. Review and explicit authorization are
   still required before deploying to the running service. Preserve existing pending
   items, their targets and schedules.

## Private state and recovery

`$DOCKET_HOME/openchamber` (default `~/.docket/openchamber`) must be owned by the current user,
0700, and not a symlink. `client.json` contains only Docket's returned token, own client ID,
trusted enrollment request origin, version and stable own dedupe key. It is written through a
0600 same-directory temporary file, fsync, atomic replace and directory fsync. Reads reject
symlinks, hardlinks, foreign ownership, oversized files and permissions other than 0600.
`identity.json` holds the stable `docket:<uuid>` dedupe key. `attempt.json` contains only
SHA-256 canonical enrollment digests (parsed pairing ID plus secret), never a link or one-use
secret. Equivalent link spellings cannot resubmit an enrollment. Legacy raw-link ledgers require
operator review: they cannot safely establish that a differently spelled link is unused.
Pairing IDs and one-use secrets must be visible ASCII without whitespace; non-ASCII forms
(including NBSP and BOM) are rejected before private-state mutation or networking, not trimmed.
This avoids a second digest for credentials that the installed host's JavaScript `trim()`
treats as the same enrollment. Human display labels are not normalized or restricted.

For a successful HTTP 200 response from the validated local request origin, only the own
client ID and `oc_client_` token shape are required before saving. Docket **atomically saves
the own token before checking any optional response metadata**, including `ok`, server URL,
fingerprint, label, device name or kind. These fields cannot discard a minted bearer or select
a different request origin. Returned metadata is nonsecret display information; missing or
different server metadata yields fixed, sanitized warnings, never raw host text in public status.
Unknown metadata is ignored. Revocation/expiry still refuses delivery, but does not erase the
saved token. Existing bindings with older display fields remain readable; names are ignored.

Concurrent enrollment is refused with an exclusive `.enrolling` lock. A crash leaves that
lock for explicit operator inspection. Existing bindings are never automatically replaced.
After a timeout, lost response, malformed response, or failed write, **do not retry the link**:
the host may already have created Docket. Inspect/revoke the Docket entry through normal UI
and arrange explicit recovery with the lead. No automated re-pair/reset command is provided.
Enrollment attempts latch host selection conservatively, even if enrollment then fails.

## Adapter and status

- Before pairing, unconfigured installations keep the generic OpenCode adapter
  and its existing discovery. After principal-consented pairing, the verified
  deployment uses Docket's own identity against the desktop origin and rediscovers
  the runtime port.
- `DOCKET_DELIVERY_HOST=openchamber-unpaired` explicitly opts an isolated daemon into current
  desktop-origin delivery without a bearer. It is not pairing, and must not replace the live
  alarm path without approval. `openchamber-paired` explicitly requires the own binding.
- Own enrollment state overrides every mode (including `opencode` and unpaired). New-code
  daemons also latch into paired mode if enrollment appears while they are running. A missing,
  unreadable, expired, revoked or mismatched binding fails visibly; it never triggers old-service
  discovery or an unauthenticated retry. An already-running **old version** must be separately
  upgraded; this enrollment cannot alter old code in memory.
- Each desktop request re-reads **only numeric `desktopLocalPort`** from existing
  `~/.config/openchamber/settings.json` and checks unauthenticated `/health` identification.
  Bearer goes only to that loopback desktop origin. OpenChamber, not Docket, supplies the
  managed OpenCode credential and routes to its current runtime. Redirects and environment
  HTTP proxies are disabled. A failed send is not repeated against another candidate.
- Before every paired session request, `GET /api/client-auth/clients` with Docket's bearer
  must return **exactly one** record matching the stored own client ID, with no expiry and
  no revocation. Label, device name, kind and branding are not identity checks; renaming the
  same own record cannot strand delivery. Otherwise no session request is made. This is the approved
  own-record revocation guard, **not proof of host-enforced authentication**. In optional-auth
  mode, a bogus bearer may get an operator-style list; an isolated sole active matching record
  could be ambiguous. A revoked record is refused even if the host permits anonymous APIs.
  There is an unavoidable guard/request race; this is not a host-enforced capability boundary.
- `/health` and `/api/v1/meta` expose `host` (adapter, mode, origin, last-check state/time and
  sanitized refusal reason). These are last-request observations, not active auth probes.
  New items store the validation host under `target.host`; admission/consumption receipts
  record the observed host under `receipt.host`, including a rollover port. No token is exposed.
  Generic OpenCode URLs must be canonical loopback HTTP(S) origins (optional trailing slash),
  without userinfo, query, fragment or other path; status stores only scheme/host/port.

## Verification limits

Fake-host tests cover enrollment, arbitrary/case-varied display labels, renamed own records,
unknown metadata, essential status/ID/token rejection, save-before-optional-check ordering,
one-use/expiry, malicious links, redirects, private ownership and atomicity, discovery rollover,
auth failures and revocation refusal. Existing scheduler
queue/steer, reconciliation and cancellation contracts remain in force through the subclass.
The scheduler polls; this change does **not** introduce an SSE consumer or claim a live event
subscription proof.

Historical Mac acceptance covered own-client delivery and installed panel
health/snapshot/removal. It is not current source/platform certification or a
fully green suite. Queue proof was natural assistant finish followed by new
user/ACK in the same active drain. Urgent proof was at a model-step boundary,
not during token generation; no continuing multi-step guarantee is claimed.
Wake lateness text is admission-time; final item lateness may include queue
consumption wait. Visual approval is not UI-click automation.
`file_stable` detects a quiet window, not semantic producer finality.
The old missing-`desktopLocalClientToken` blocker is historical; supported
own-client pairing needs no borrowed secret. Optional loopback auth is not a
host-enforced capability. Real revocation remains unproven; do not destroy the
final credential to test it. No automated app restart is authorized.
Stop/rethink after two failed host proofs; this documentation authorizes no new
live probe or enrollment.

The historical browser-era full suite was not green, with two browser-startup
failures before panel code. Browser/CDP testing remains stopped after installed
Chrome's updater escaped isolation, until containment is independently verified.
See `README.md` for service stop/start/uninstall and
consistent-backup/reviewed-recovery cautions; never blindly restore a database.
