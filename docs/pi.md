# Optional Pi companion adapter

The adapter uses the [pi-a2a 0.2.0 companion contract](https://github.com/batteryshark/pi-a2a/blob/ceafe6f8f2c3e1d9d2e85845feac96f13dc30853/README.md).
It requires an existing persistent Pi RPC recipient and a compatible Node runtime.
Queue/no-reply saved intake is supported; unattended availability, task execution
and Docket busy-steer integration are not established. For `A2A_BUSY` after a
possible send, inspect status using the original caller/key rather than resending.

For Pi, `delivered` means only consumed / `saved_exact` intake; execution and
provider readiness remain unknown. It does not prove model understanding,
authentication or task success. The companion's own busy-steer verification is
separate: Docket busy-steer integration is not established by this queue use.

## Selection and private configuration

Explicit per-target create-body `"adapter":"pi"` selects Pi in the **same normal
installed daemon**. CLI timer/alarm/recurring/watch offer `--adapter pi`; the four
ordinary MCP create tools offer optional `adapter`. Omission (or `opencode`)
preserves normal OpenCode/paired delivery and never reads Pi config. Existing
OpenCode and Pi records coexist; the scheduler never switches its shared
default adapter. No fallback occurs from selected Pi to OpenCode. Missing or
invalid Pi configuration refuses only Pi operations. `DOCKET_DELIVERY_HOST`
continues selecting only the existing default OpenCode/paired host; Pi is not
a global service mode.

The fixed file is `pi-companion.json` in Docket's existing platform state root,
read through the existing private owner-file guard. No config is created or
migrated by the adapter. Its exact shape, with fictional paths, is:

```json
{"v":1,"node":"/opt/reviewed-node/bin/node","cli":"/opt/reviewed-pi-a2a/cli.mjs","state":"/private/pi-mailbox"}
```

All paths must be absolute and normalized. The installation operator must
review a trusted supported Node executable (Node 22.19.0+), the exact released
installed companion entry, and the canonical private shared `PI_A2A_STATE` used
by the receiving Pi. Path syntax is not provenance, a version check, or proof
of a stable provider namespace. Docket does not inspect that mailbox or repair
its permissions. Configuration is operator authority, never an API/reminder
field or client-supplied argv. No executable is taken from reminder data.
No service renderer or Mac six-variable installer environment contract
is extended here. Per-target selection needs no new service environment capture.

## Operator setup

These are setup instructions, not permission to start a held host or change an
existing deployment. Docket never launches Pi; the receiving Pi CLI process and
its persistence are the operator's responsibility.

1. Use trusted Node **22.19.0+** and **Pi 1.0.4**. Review and install the published
   **0.2.0** companion normally in the receiving project, pinned to the released
   commit (Pi may reconcile package dependencies):

   ```sh
   pi install git:github.com/batteryshark/pi-a2a@ceafe6f8f2c3e1d9d2e85845feac96f13dc30853 --local
   ```

2. Select one fresh, canonical, absolute, non-symlinked private `PI_A2A_STATE`
   outside the checkout for both the receiving Pi and companion. Do not reuse
   old schema state or move unresolved work. Start the persistent receiver using
   normal installed-extension discovery, for example on POSIX:

   ```sh
   export PI_A2A_STATE=/private/pi-mailbox
   pi
   ```

   Alternatively the upstream reviewed-clone invocation is
   `pi --no-extensions --no-mcp -e /opt/reviewed-pi-a2a/index.ts`. Choose one loading
   route; never load an installed copy and an explicit copy together. Do not use
   `--no-session`, or open the same Pi transcript in two processes. On Windows,
   use the trusted supported Node with the reviewed installed Pi JavaScript entry,
   not an older PATH Node or a `pi.cmd` launcher for the native machine companion;
   follow the upstream native launch/security policy.
3. Provision `pi-companion.json` in the **existing selected Docket state root**
   using its private storage guards. Its only fields are the exact v1 shape
   above. `node` is the trusted executable, `cli` the installed companion's
   absolute `cli.mjs`, and `state` that same canonical mailbox. Docket supplies
   the companion's `PI_A2A_STATE` from this binding; no daemon environment addition
   is needed. Keep the config and paths private, not in reminder API/MCP bodies
   or reminder argv.

   For an authorized initial create, run this Python snippet with the installed
   Docket console interpreter from its source root, with the same `DOCKET_HOME`
   as the daemon. Replace the fictional absolute paths first; on Windows use
   reviewed fully qualified drive paths (for example `C:\ReviewedNode\node.exe`,
   `C:\ReviewedCompanion\cli.mjs`, `C:\PrivateLeaf\pi-mailbox`), expressed as raw
   Python strings or with escaped backslashes in the snippet.

   ```python
   import json
   from docket.platform import create_private_file, state_root

   config = {"v": 1, "node": "/opt/reviewed-node/bin/node",
             "cli": "/opt/reviewed-pi-a2a/cli.mjs", "state": "/private/pi-mailbox"}
   create_private_file(state_root() / "pi-companion.json",
                       json.dumps(config).encode("utf-8"))
   ```

   This existing function exclusively creates; it never overwrites an existing
   config. POSIX requires owner-only directory 0700/file 0600. Windows Docket
   uses current-user ownership and protected private ACLs, with native reparse,
   handle and link checks. Do not substitute a generic write followed by ACL
   repair. If creation fails or leaves an uncertain result, stop for operator
   reconciliation; do not delete/retry or weaken the guards.
4. Obtain your owned target's `pi/<id>` through the companion's public interface.
   Public capabilities and lookup must report external-v1 support and a live,
   capable persistent recipient; they reserve nothing. Then select Pi explicitly:

   ```sh
   bin/docket timer --in 20m --adapter pi --session pi/fake_receiver --reason "check progress"
   ```

   Replace the fake target with your own returned address. On Windows use the
   recorded console interpreter and source root in the [service runbook](services.md).
   MCP create tools and HTTP create bodies accept optional `"adapter":"pi"`
   with `"session_id":"pi/fake_receiver"`. Missing/invalid config or an unavailable
   target refuses creation rather than promising delivery. Omit `adapter` to keep
   OpenCode behavior; there is no fallback from Pi.

For Windows, the companion requires local fixed NTFS and an existing safe parent
for a new protected leaf. Store roots/files/locks remain current-user-owned;
SYSTEM/Administrators grants may be allowed, not other ordinary users. The
upstream Administrators-owned transcript-evidence exception is read-only, not
Store authority. TrustedInstaller exceptions apply only to approved OS ancestor/
launch roles, never payload ownership. Unsupported helpers, ACLs or paths fail
closed. Use the upstream security policy, not a generic provisioning/repair
recipe. The OS account remains the trust boundary, not protection against
malicious same-user code.

## Bounded companion exchange

The only child argv is `[configured_node, configured_cli, "machine"]`, without
a shell, message text, caller, request ID or target. One strict JSON stdin frame
then EOF carries the request; stdout is one newline JSON object, limited to
8192 bytes. The companion's ten-second **input-through-EOF** limit is separate
from Docket's finite **30-second local exchange deadline**, plus at most a small
0.5-second direct-child cleanup budget. Neither is a published whole-command
latency SLA. Node injection variables are not forwarded. Stderr and exception
text are not shown to the user.

The cached runner uses raw/unbuffered pipes, no Popen context manager, and two
daemon I/O threads that own their pipe operations/close. Every caller wait/join
has a finite remaining budget. Cleanup terminates only the exact created child;
it does not discover or terminate descendants. Inherited pipe handles can remain
blocked after that child exits: the scheduler returns uncertainty rather than
waiting indefinitely. One retained active record prevents further children or
I/O pairs until the child is reaped and its I/O clears. There is no monitor,
reaper service, process-family helper or universal descendant-cleanup claim.

## Targets, identities and truth

- `--adapter pi --session pi/<id>` addresses an existing Pi target.
  Pure capabilities and lookup must show external-v1 support and a live capable
  target before creation; the same snapshot is revalidated at firing. Lookup
  reserves nothing. Directory pinning and `session_idle` watches are rejected.
  File/process/URL watches retain the ordinary latched wake route.
- The existing database `delivery_meta` caller (`docket-` + 32 lowerhex) is
  reused. Each original item/occurrence reserves one `msg_` + 32 lowerhex ID.
  Caller, target, exact text, delivery, no-reply and configuration binding are
  frozen in the existing durable request envelope before dispatch. A changed
  Node/entry/state binding cannot redirect an old reminder, including one
  created before its first firing. Private installation paths are not returned
  in public item target metadata.
- Ordinary delivery is native queue/followUp; urgent is steer. `expectReply`
  is always false. No sender session, reply subscription or Pi launch is made.
- Admission, claim, injection and observation are not consumption or execution.
  Only a valid consumed receipt with `saved_exact` saved-intake evidence earns
  delivered. Execution always remains unknown. Historical uncertainty and
  positive facts are retained; later missing/unavailable checks erase neither.
- Malformed, truncated, oversized or timed-out responses and partial commits
  remain uncertain. Missing status is not permission to resend. Docket uses the
  original caller/ID for status/cancel and never reads private transcripts.
  `namespace_verified` stays false: existing same-daemon pre-dispatch recovery
  remains conservative; possible sends are status-only, even though Pi dedups.
- A documented terminal `failed` receipt before claim is failed, not consumed or
  automatically resendable. The original possible-send barrier remains. Earlier
  positive claim/injection/consumption facts cannot be downgraded by later
  contradictory failure snapshots.
- A cancelled unknown-ID tombstone is a barrier, not evidence of admission.
  `TOO_LATE` means irreversible claim, not consumption or removal. Cancellation
  cannot clear a native queue after that boundary.
- Capacity is globally shared: 50,000 permanent identities and 128 active
  payloads, also subject to byte bounds. There is no TTL, eviction, replacement
  ID or force-duplicate workaround. Sustained five/minute traffic can exhaust
  the permanent identity budget in under seven days.

## Status and recovery

Inspect the item and its delivery facts using [delivery status](delivery-status.md).
After any possible send, retain the original caller/request ID and use only
status/reconcile, not a blind resend, replacement ID or another target. A transient
busy check may require one later authorized same-key status; this is not an
automatic polling loop or a guarantee that busy will clear. Preserve earlier
positive facts and historical uncertainty even when later saved intake resolves.

Stale/incomplete locks fail closed. There is no public lock-repair command or
cancellation bypass; do not remove locks, edit state or inspect mailbox/transcripts
as a Docket recovery shortcut. Cancellation uses the same identity and lock and
cannot retract after irreversible claim. Runtime-only config paths stay private;
companion machine responses are sanitized and never expose payloads, paths or raw
errors. Docket public target metadata does not expose the installation binding.
