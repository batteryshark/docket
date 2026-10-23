# Fixed native handler backend

`docket.sandbox_native.configured_sandbox(private_config_path)` is the only
installed factory. Its private, owner-only JSON requires exactly:

- `version: 1`, `backend: "seatbelt-opencode"`
- absolute `state_root`, `toolstack_root`, `broker_db` (inside state_root),
  `events_plugin` (installed native profile plugin directory)
- distinct `handler_port`, `gateway_port` (loopback only)
- `timeout` (1–3600 seconds; effective deadline can only decrease)
- `installation: {baseline, profile, available_tools, remaining_budget}`
- `approval: {mode: "refuse"}` or explicitly test-only `{mode: "local-test"}`;
  real Nod uses `{mode: "nod", url, issuer_token_file, channel}` with a separately
  authorized broker-owned issuer reference, never a borrowed client credential.

Version1 is the legacy explicit **fixture test** installation. Version2 adds
`broker_mode` (`fixture` or `installed`) and `broker_config_ref` (null in fixture
mode; absolute owned0600 file beneath private0700 parent in installed mode).
Installed mode uses the unmodified Tool Stack BrokerContext, Registry.from_sources,
HttpRuntime, explicit ToolState, AuditLog, NodSurface and RateLimiter behind
ScopedBroker. It never starts or modifies another project's workloads.

The private broker configuration has exactly `version:2`, `mode:"installed"`,
`tools_root` (absolute or null), `tool_dirs` (explicit absolute directories),
`toolyard_state_file` (explicit owned0600 state, no fallback), `policy`,
`fixed_args`, `data_fields`, `tool_timeout_seconds` (1–60), and `rate_limit` (1–120/minute).
Policy uses Tool Stack's normal `{tools:{tool:{operation:allow|review|deny}}}`.
`fixed_args` maps `tool.operation` to installed authority/resource values.
`data_fields` maps EVERY installed operation to an explicit reviewed list of
non-interpreting ordinary data fields (possibly empty), checked against the
registry's actual schema. Missing contracts are refused. A benign field name or
string type is NOT by itself evidence of data-only semantics.

Only the intersection of installed registry, broker policy, native baseline and
profile is exposed. Names are `toolstack_<tool>__<operation>` with collision
refusal. A fixed plugin derives schemas from registry descriptors, excludes all
fixed keys, and accepts only bounded ordinary scalar data. The gateway refuses
fixed-key overrides/unknown or missing arguments, then merges trusted fixed
values. REST currently requires ALL arguments, including the full body, fixed
installed data and an empty data_fields list: the normal forwarder defaults to
interpreting `{{secret:NAME}}` in text bodies/headers. Event-controlled secret
template syntax is rejected recursively for every transport BEFORE approval
persistence/execution, including review reasons. No automatic substitution-disabled
inference is made. Normal API/MCP transports expose only reviewed bounded scalar
data contracts; unsupported contracts fail closed.

Resource-bearing paths/repos/URLs/hosts/commands/argv/env/config parameters
must be fixed; unscoped dangerous operations, complex model-selected arguments,
raw secret export and unsupported shapes are refused, not advertised as safe.
`refresh_one` requires its secret-cache name fixed; refresh capabilities still
require explicit installed policy and native baseline. Deployment owners must
review actual workload semantics: a misleadingly named "data" parameter that
executes code is not made safe by this adapter and its class must not be installed.

Config/TOML descriptor/endpoint/schema hashes are frozen per deployment, rechecked
before actions and approval resolution, and pinned into each native descriptor
snapshot. Changes refuse further calls until redeployment. Channel credentials
may rotate only at their explicit private ToolState reference, remaining server
side. Review is never promoted to allow; installed review uses real NodSurface
or fails unavailable. Local pending surfaces remain fixture-test-only.

Models are explicitly installed
`xai/grok-build-0.1` or `xai/grok-4.7`, HTTP only, with profile/baseline token caps.
The privileged gateway uses normal own-account xAI subscription auth in memory;
the handler receives no provider auth. Native provider `settings.timeout` is
documented in V2 providers documentation; new allocations cap it at 120 seconds
and the remaining absolute handler deadline. No existing test admission's budget
or counter is changed by this code.

The finite gateway requires an explicit absolute installed `toolstack_root`;
there is no ambient/personal import fallback. It reads only the executing
account's canonical OpenCode credential DB through an encoded read-only SQLite
URI with query_only enabled, requiring active, unexpired xAI OAuth. No credential
refresh/write/copy or event-selected auth path is supported.

Each allocation owns its **own finite gateway and OpenCode Fleet services**,
registered before start under `docket-test`. Neither depends on the temporary
47961 owner host nor 47962 gateway. Allocation ID, deterministic native SID,
goal, service names/argv and receipt persist before compute. The frozen prepared
handler class stays unchanged; `bind_handler_instance` records the trusted actual
SID/directory/goal in secret-free read-only control files before hooks activate.

The process itself runs Seatbelt, not just its tools. Only private workspace,
system runtime bootstrap and exact installed plugin/manifest reads are granted.
Only its own API listener and model/broker gateway TCP endpoint are permitted.
CPU, wall time, file/output size, FDs and process resource ceilings are hard
limits. No whole-machine memory isolation claim is made.

`lookup(delivery_id)` reads durable identity without allocating. `status(binding)`
checks a unique exact native user admission/body and its first idle terminal;
additional user/synthetic/shell input invalidates completion. `receipt(binding)`
returns detached lifecycle/terminal/output evidence, never credentials. Finished
requires consumed + exact terminal + native quiet + successful owned prune.
Cleanup revokes first, stops the exact owned Fleet parents, verifies recorded
processes gone, then removes only its marked workspace. Audits/transcripts and
uncertain effect records remain outside the workspace. Unknown allocation/stop
states retain compute ownership, not permission to replay.

Separate native acceptance covers this backend's actual installed scoped tool
execution, exact terminal witness, token AND caller revocation, quiet compute and
automatic workspace pruning. A normal live scheduler handler also completed the
bounded journal operation. The exercised route is xAI/grok-4.7 HTTP; the live
default class is journal-only. Unit/contract/OS denial tests alone do not establish
these outcomes, universal platform/provider support or a real principal Nod approval.
