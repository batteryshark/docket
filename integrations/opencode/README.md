# Docket native wake profiles

Authenticated queue-to-existing-owner and fresh installed-handler execution are
accepted, including observed injection rejection, bounded model requests and
automatic token/caller revocation and pruning. The normal live scheduler also
completed a journal-only handler. These are bounded demonstrations, not universal
provider/platform guarantees; no principal approval is fabricated.

## Operator entrypoints and requirements

Use Python 3.11+ (accepted checks used 3.13) and source containing the native event
adapter/worker, not just the older intake modules. Compatible OpenCode V2 hooks
and APIs are required; native acceptance used 2.0.32. The concrete handler backend
is macOS Seatbelt with the installed OpenChamber OpenCode executable and Fleet;
it does not supply Linux/Windows/Pi handler isolation. Install the matching
`docket-events-native` 0.1.0 source package, not a registry package of that name.

With reviewed owned0600 configs under owned0700 `~/.docket/events/`, the foreground
form is (do not run alongside an installed daemon):

```sh
bin/docket serve --events-config "$HOME/.docket/events/subscription.json" \
  --event-host-config "$HOME/.docket/events/host.json" \
  --event-journal-config "$HOME/.docket/events/journal.json"
```

`daemon` accepts the same flags. Journal config is optional except for the journal
class. Preserve existing service arguments/state when integrating; service install
does not provision these manifests or native baselines. Offline operator command
`bin/docket events install --config <absolute-private-manifest> --database <absolute-db>`
installs immutable configuration only, not a running worker. Reopening the same
manifest is idempotent and does not clear revocation; replacement requires an
explicit upgrade, not editing event data or resetting the DB.

Manifest V2 is exactly `{version:2,publisher:{id,token_sha256},subscription:{id,
event_type,profile:{action,baseline,profile}}}`. The nested baseline/profile schemas
are below. `POST /api/v1/events` accepts a separately provisioned publisher bearer
and JSON `{id,type,payload:{<installed field>:<string>}}` (16KiB maximum); type and
fields must match the installed subscription. Retain the same ID/body after an
uncertain acknowledgement; do not mint a new event to replay an effect. Admin-only
GET routes are `/api/v1/events`, `/api/v1/events/deadletters`,
`/api/v1/events/<delivery-id>` and `/api/v1/events/outcomes` (journal required).
CLI inspection commands are `events list --json`, `events deadletters --json`,
`events inspect <delivery-id> --json` and `events outcomes --json`.
No event publishing or inspection MCP tool is registered by this package.

## Explicit host factory

`docket.event_host.configured_event_adapter(host_config)` takes:

```json
{"version":1,"url":"http://127.0.0.1:47961","binding_root":"/absolute/private/bindings"}
```

No discovery, default host, global authentication lookup, or `resume:true`.
Optional fourth config key `server_auth_file` is an absolute owned0600 file path;
the trusted adapter reads the password only to construct an in-memory Basic header
with fixed username `opencode`. Redirects are not followed. Never serialize that
header/password into prepared records, bindings, events, logs or receipts.
The events lane loads this config privately, separately from its V2 manifest.
Factory refuses a non-loopback URL. Server auth/isolation is the deployment owner's
responsibility; do not expose an unauthenticated test port beyond its approved scope.

Methods:

- `prepare(config,event_data,admission_id)`; config is exactly
  `{action:'owner'|'handler',baseline:<installedobject>,profile:<installedobject>}`.
- `validate(prepared)` returns exactly target, owner_generation, profile_id,
  profile_version, baseline_version, model, role, stopped, revoked.
- `dispatch(prepared,stable_id,*,lifecycle=None)` returns existing `Receipt`.
  `stable_id` MUST equal `prepared.admission_id`, not the queue delivery ID.
- `reconcile(prepared,stable_id)` / `status(...)`: admitted, consumed, missing,
  unavailable. Exact user type, ID and body required. Never infer handler_finished.

Optional trusted lifecycle is exactly `{delivery_id,deadline,lease_until,generation}`,
with finite numeric UNIX-epoch seconds (booleans/ISO strings refused), converted
to aware UTC internally, `now < lease_until <= deadline <= now+7days`.
It is outside prepared/event data. Owner path validates then ignores it. Handler
action delegates to an explicitly installed Sandbox and persists admission->delivery
intent before allocation; existing intents reconcile through lookup, never relaunch.
Optional private `sandbox_config` hostconfig path selects only the fixed constructor
`docket.sandbox_native.configured_sandbox(path)`, not a module/command from JSON.
Missing concrete backend or lifecycle recovery API fails closed as unavailable.
Constructor injection `EventHost(...,sandbox=trusted_object)` is for explicit trusted
composition/tests; a fixture backend is not OS/native handler proof.

## Installation files

Keep `binding_root` owned 0700, JSON files 0600, regular single-link files. External
trusted Docket writes it. Seatbelt may grant the host read-only access: these are
policy facts, not secrets. Deny model/tool writes to root AND its parents, including
rename/symlink replacement. There is no plugin-vs-tool OS identity distinction.

- `installation.json`: `{baseline,profile,available_tools,remaining_budget}`.
- `owner.json`: `{target,owner_generation,goal_id,goal_generation,stopped:false,revoked:false}`.
- `<native-session-id>.json`: durable EventHost-created delivery binding. Must exist
  before a model call; missing/malformed fails closed. One in-flight owner binding;
  no overwrite or lease expiry on uncertain delivery. Different admission remains
  retryable `EventHostBusy` until authenticated native transcript proves the exact
  old user ID/body followed by terminal execution marker, no other inputs, no active
  owner and empty inbox. Under per-owner flock, preserve old binding plus terminal
  witness in immutable `archive-<old-admission>.json` before atomic replacement.
  Native per-admission budget storage is never deleted/reset. This is turn-binding
  retirement, not business completion and never `handler_finished`.

The baseline has exactly:

```text
version, target:{session_id,directory}, owner_generation, goal_id,
goal_generation, role, authority:[names], policy:{tool:deny|review|allow},
secrets:[broker-reference-NAMES], models:[provider/model],
budget:{max_steps,max_output_tokens,total_output_tokens}, context_paths:[absolute files]
```

The profile has exactly:

```text
id, version, baseline_version, role, authority:[subset], policy:{subset},
secrets:[subset of reference NAMES], model, budget:{same three keys},
template, fields:[exact event keys], required_tools:[names], context_paths:[subset]
```

`goal_generation` preserves the native goal's stable `createdAt` JSON value, NOT
updatedAt. Actual App integer epoch-ms values must be positive safe integers;
booleans, floats/nonfinite, nonpositive and unsafe integers refuse. Legacy bounded
fixture strings remain supported without coercion. Native comparisons preserve type.

For handlers the baseline instead has `target:{handler_class:installed_id}`, with
`owner_generation` denoting the frozen class deployment. It omits owner-only
goal_id/goal_generation. Prepared includes `action:'handler'`, retains that class
target forever and never acquires a native SID. After no-model allocation, trusted
runtime calls `bind_handler_instance(prepared, installed_baseline, session_id=...,
directory=..., environment_id=..., generation=..., goal_id=..., goal_generation=...)`.
The result is secret-free instance.json, installed read-only BEFORE native hook
activation/model: version, handler_class, baseline_version, profile_id/version,
owner_generation, target:{actual session_id,directory}, environment_id, per-run
generation, actual goal ID/createdAt, admission_id, canonical prepared_hash.
Hash is SHA256 of UTF8 JSON sorted keys/compact separators/ASCII/no NaN. No prepared
mutation. Native hook gates that exact instance plus class policy. Runtime owns
actual Seatbelt/Fleet allocation, broker credential, supported model selection,
exact user admission, terminal witness and revoke-first quiet cleanup.

Handler finished requires public Sandbox.receipt(binding): phase pruned, consumed,
trusted_completion, retained output_manifest and native_terminal with actual SID,
exact admission_id, SHA256 of raw UTF8 briefing, terminal_id/outcome, not_active=true
and empty_inbox=true. Idle/consumption alone are insufficient. Backend's allocation
is recovered via public lookup(delivery_id), never launch-on-recovery.

Native goal ID/createdAt and authoritative **active** status are required before
model and tool calls. Ordinary accounting timestamp changes do not invalidate it.
Paused waits are not auto-resumed. Hook-owned owner generation/revocation is checked
again after approval waits. No metadata is written. No atomic native stop CAS:
a stop can race the final native lookup and already-started effects remain possible.

No authority/event interpolation: up to 32 exact string fields, 4000 bytes each,
16KiB escaped total; context up to four exact installed regular files, 4096 bytes
each; symlinks refused. Event/context JSON escapes newlines and angle delimiters.
Injection resistance ultimately relies on execution policy and sandbox/broker gates.
Installation provenance is a caller/deployment requirement, not authenticated by
the validator's Python argument names or file modes alone.

## Native load

Load this directory through the supported V2 plugin package mechanism **last** in
the explicitly provisioned native host configuration:

```json
{"plugins":[{"package":"/absolute/docket/integrations/opencode","options":{"bindingRoot":"/absolute/private/bindings"}}]}
```

Local directory source loader probes `server`/`index` directly, unlike published
package exports. Both `index.js` and `index.mjs` re-export the implementation;
grant exact source reads plus directory/file-existence traversal. Native activation
must show active `docket-events` (configuration presence is insufficient).

Install trusted role/baseline, active native goal and hooks before enrolling an
existing owner; never silently attach to existing principal leads. Preserve its
agent; EventHost uses the supported `/api/session/{id}/model` operation before the
exact-ID queued `/prompt`. This plugin registers no tools. The sandbox fixture
must expose only its approved broker tool with `codemode:false`. Do not enable
generic shell/read/edit/patch/subagent/CodeMode bypasses. Broker enforces actual
operations, review/Nod, scoped credentials and revocation independently.

Hooks: prompt verifies exact admission/body, context narrows schemas, execute.before
independently checks tools/owner, permission.evaluate preserves review as ask.
Auxiliary compaction/generate and WebSocket are refused; title is deterministic.
No automatic retry. Context exceptions are host defects rather than a documented
typed prompt-rejection API; native fixture acceptance must prove no request escapes.
Tool execute.before exceptions convert to native Tool.Error (2.0.32 embedded source).

## Budget meaning and limits

`max_steps` currently bounds physical **primary HTTP model requests**, conservatively
including retries; total output reserves the request cap durably *before* send.
Ambiguous/failing requests do not refund reservations. No input-token, money, time,
tool-count, or full goal budget claim. Remaining admitted budget comes from trusted
installation; per-admission host storage reservations survive restart. Cross-delivery
remaining-budget accounting remains with the admission owner.

Current supported capped bodies are Anthropic Messages `max_tokens` and xAI Responses
`max_output_tokens`; final HTTP hook
sets it no higher than output/remaining caps. OpenAI subscription deliberately omits
maxTokens and is refused here. Google has native capped protocol code but no approved
installed subscription route identified. xAI's default WebSocket transport is
refused. Install `providers.xai.settings.transport:"http"` explicitly. The exercised
hard-cap route is `xai/grok-4.7` HTTP, with two requests capped at 512 output tokens
each and 1024 reserved total. The final hook also refuses wire model aliases and
provider-hosted tools; other protocol code is not live compatibility proof.

Later trusted plugins could rewrite tools/request bodies after this gate: install
last under a fixed approved plugin set, not arbitrary project plugins. Never claim
broker policy or OS isolation based on schema hiding alone.

## Existing own milestone tool

Reuse the kit's `services/docket/milestone.mjs` `createMilestonePlugin(binding)`;
reference Docket's `docket/milestone.py` rather than duplicating it. The trusted
wrapper fixes pythonPath, sourceRoot, home, callers with native sessionID,
agentID/exact directory, and helperFiles SHA-256 sourceHashes. Production home
selects the enrolled private roster; tests use only temporary rosters.
Native baseline may permit `docket_milestone`
only if already installed, with resource `own`; no generic roster or helper shell grant.
The installed kit wrapper's actual reset own-row receipt and narrow refusal checks
were already accepted by the lead. Do not reopen that acceptance with another
native invocation, reset/Dream/iPhone/Culdcept write, or receipt replay. This source
retains unchanged helper tests and narrow binding contract; not all leads invoked
the tool, and arbitrary noncooperating roster-writer CAS is not claimed.

## Local checks (not native acceptance)

159 selected checks were accepted with the integrated source, covering core,
event/auth/profile/factory/terminal and journal/API/actual Tool Stack HTTP contracts.
`node --test tests/test_docket_events.mjs` provides focused hook unit checks.
Offline/fake-process checks are not native proof. Separate accepted native evidence
covers both actions, actual installed scoped tool execution, observed injection
rejection, hard caps and automatic handler cleanup; failed historical attempts
remain failures. The live class is journal-only. Tool Stack/Nod pending/reject
contracts do not establish a real principal approval or arbitrary operation safety.
