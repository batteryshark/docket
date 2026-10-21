# Per-user services

These are reference instructions for an operator-authorized deployment, not
permission to start a stopped service or resume a safety-held host. Docket service
commands manage Docket only, not the agent host. See [delivery recovery](delivery-status.md)
before maintenance around outstanding wakes. Stopping prevents due-time fires;
it does not retract an admitted message or prove that it was not consumed.

## Deployment and private state

Use Python **3.11 or newer**. The Python package declares `tzlocal>=5.0` and
`tzdata` on Windows; provision these in the selected runtime beforehand.
The Unix core uses the standard library. Linux also requires a working systemd
user manager; Windows uses the system Windows PowerShell, Task Scheduler COM,
and local WMI process metadata. Install does not copy source, create a virtual
environment, or install dependencies. No global PATH, execution-policy, module,
elevation or ACL repair is part of this procedure. If a guard refuses, stop and
resolve the deployment with the operator; do not bypass it.

Choose a trusted, persistent source root containing `docket/__main__.py`, and a
persistent compatible interpreter. Linux and Windows require explicit
`DOCKET_SERVICE_ROOT` and `DOCKET_SERVICE_PYTHON` for install; temporary and known
development worktree locations are refused. Keep code and runtime available at
their installed locations. Treat the deployment as immutable while registered.
Windows fingerprints package Python source and refuses changed code on control
or launch. The interpreter and its dependencies remain trusted inputs.

Default private state roots:

- macOS and Linux: `$HOME/.docket`.
- Windows: `%LOCALAPPDATA%\Docket`; `LOCALAPPDATA` must be fully qualified.
- `DOCKET_HOME` explicitly selects a different dedicated private root, with `~`
  expanded. Use an absolute path and select the same root for daemon and clients.
  Changing it selects different state; it is not a migration or recovery method.

State includes `docket.db` and SQLite sidecars, `token`, `server.json`, pairing
state when configured, and delivery history. POSIX state is owner-only (directory
0700, private files 0600). Windows checks owner/private ACLs, reparse points and
file link counts; it does not silently repair broad existing permissions.
Keep logs and service records private too. Do not print tokens or put credentials
in arguments; existing [own-client pairing](../PAIRING.md) remains unchanged.

Moving or updating a registered deployment requires deliberate operator-controlled
reconfiguration preserving the service record and state. There is no upgrade
command. In particular, a new explicit Windows root/interpreter is refused while
an existing valid private manifest remains, **even after uninstall**, which retains
that manifest. Do not delete, edit or rename it as an upgrade shortcut, invent a
replacement identity, or relax guards. Preserve the reviewed deployment so guarded
stop/uninstall remains possible; arrange a reviewed reconfiguration separately.

## Optional Pi recipient

The [Pi companion adapter](pi.md) is an optional per-target route in the ordinary
Docket daemon, not a service mode or an agent launcher. Default OpenCode/paired
selection and the existing service environment contract stay unchanged; no
fallback is added. The operator must separately run an existing persistent Pi
with the published companion and the same canonical private mailbox bound by
Docket's private `pi-companion.json`. Missing/unsafe configuration refuses Pi
operations. Follow the Pi guide's guarded exclusive config creation and native
security policy; do not provision with generic writes followed by ACL repair.

Verified native Windows Docket queue saved intake does not prove Pi boot/logon
startup, unattended availability, OS-restart recovery, provider readiness or
execution. Docket service start/stop/install never manages Pi. Coordinate its
lifecycle separately around outstanding requests; stopping either process is not
retraction or permission to resend.

## macOS: existing LaunchAgent

The existing `local.docket` path is unchanged:
`$HOME/Library/LaunchAgents/local.docket.plist`. From the persistent checkout:

```sh
cd "$HOME/Applications/Docket"
bin/docket service install
bin/docket service status
bin/docket service stop
bin/docket service start
bin/docket service restart
bin/docket service uninstall
```

These are individual actions, not a script to run in sequence. Install writes
the plist, boots out a prior job and bootstraps it in the current GUI user domain.
Start bootstraps an unloaded plist or kickstarts a loaded job. Stop boots it out
but keeps the plist. Restart uses `kickstart -k` on the registered job. Uninstall
boots out and removes the plist, not state. Status reports a sampled launchd
state/PID line, not HTTP or delivery health.

Install pins the running absolute Python >=3.11 interpreter without resolving
its executable symlink (preserving virtualenv identity). The plist invokes
`[interpreter, '-m', 'docket', 'daemon']` with the package root as WorkingDirectory.
`DOCKET_PYTHON` may select the installer interpreter via `bin/docket`; it is not
stored or reselected at daemon startup. The serialized plist safely escapes XML
and captures exactly PATH, HOME, USER, LOGNAME, canonical DOCKET_HOME and
DOCKET_DELIVERY_HOST. Supported modes are `auto`, `opencode`,
`openchamber-paired` and `openchamber-unpaired`; credentials are not captured.
Invalid interpreter/state/delivery configuration or serialization refuses before
plist writing or launchctl. Bootstrap failure is not reported as install success.

Only Mac install accepts an explicit already-absolute DOCKET_HOME symlink alias
to an existing directory. Its canonical target must pass the unchanged dedicated
directory, owner and mode-0700 guards; broad permissions are refused without
repair. Dangling/relative aliases are refused. Default selection and ordinary
new-private-directory handling are unchanged. This does not loosen ordinary
store access: use the pinned canonical path for clients. Contents are retained,
not migrated. Moving code/runtime or changing captured settings requires a
deliberate reinstall; existing registrations and running services are not updated
by a source change. Reinstall is a separate operator-authorized action.

Stdout/stderr use the selected canonical state directory (default `$HOME/.docket`):

```sh
# DOCKET_HOME here must be the installed canonical state path.
tail -n 40 "$DOCKET_HOME/daemon.out.log" "$DOCKET_HOME/daemon.err.log"
```

## Linux: systemd user unit

Use the ordinary owning user's login with its user manager/bus available. No
system service or sudo is used. Login starts the enabled user service; operation
after logout or before login depends on the machine's existing user-manager/linger
policy. Install does not enable linger. Do not assume boot-time availability.

Example paths below must already hold the trusted source and runtime. Run from
the source root so `-m docket` imports that package:

```sh
export DOCKET_SERVICE_ROOT="$HOME/Applications/Docket"
export DOCKET_SERVICE_PYTHON="$HOME/Applications/DocketRuntime/bin/python"
export DOCKET_HOME="$HOME/.docket"
cd "$DOCKET_SERVICE_ROOT"
"$DOCKET_SERVICE_PYTHON" -B -m docket service install
"$DOCKET_SERVICE_PYTHON" -B -m docket service status
"$DOCKET_SERVICE_PYTHON" -B -m docket service stop
"$DOCKET_SERVICE_PYTHON" -B -m docket service start
"$DOCKET_SERVICE_PYTHON" -B -m docket service restart
"$DOCKET_SERVICE_PYTHON" -B -m docket service uninstall
```

Choose actions individually. Install writes the owned `docket.service` under
`$HOME/.config/systemd/user` (or absolute `$XDG_CONFIG_HOME/systemd/user`), reloads
the user manager and enables/starts the unit. It retains the explicit venv
interpreter path, working directory and `DOCKET_HOME`. Start/stop/restart call the
corresponding systemd user action. Stop leaves the unit enabled for a later login;
uninstall stops, disables, removes the owned unit and reloads, retaining state.
Foreign units, symlinks, differing owned definitions, unexpected registrations and
drop-ins are refused rather than overwritten.

Status reports installed/enabled/active/load labels. An owned loaded but inactive
unit can return success; this does not establish a running healthy daemon.
Output goes to the user journal, not Docket's Mac/Windows log files:

```sh
journalctl --user -u docket.service -n 40 --no-pager
```

Journal access/retention is a system policy, not a Docket audit guarantee. The
unit uses bounded failure restart settings, not a guarantee of recovery from
every failure.

### Optional OpenCode boot-start companion

Unattended OpenCode wakes require the agent service to be available as well as
Docket. `opencode service start` starts OpenCode on demand; it does not register
OS boot startup. Docket's source install does **not** create the companion below
or manage agent lifecycles. This is an optional, operator-authorized Linux recipe,
not a guarantee of startup for every harness. If another manager already starts
the same OpenCode service, do not create a duplicate boot manager.

As the owning user, create `docket-opencode-start.service` in the same user-unit
directory described above. Replace `/path/to/installed/opencode` with the absolute
path to the persistent, installed official OpenCode executable, not a shell alias:

```ini
[Unit]
Description=Docket local OpenCode startup
Before=docket.service

[Service]
Type=oneshot
ExecStart=/path/to/installed/opencode service start
RemainAfterExit=yes
TimeoutStartSec=60

[Install]
WantedBy=default.target
```

After authorized creation, reload and enable/start this unit normally:

```sh
systemctl --user daemon-reload
systemctl --user enable --now docket-opencode-start.service
```

Boot without login still depends on the existing user-manager/linger policy.
Arrange any policy change separately with the authorized operator; do not blindly
enable linger or use sudo. The accepted Linux demonstration left existing linger
unchanged: the first start preserved a healthy OpenCode process, and one reboot
ran the companion successfully once, automatically started OpenCode and Docket,
and delivered the retained timer with one saved exact tool-free ACK and a consumed
receipt. This is scoped evidence, not universal boot or delivery assurance.

For visibility, choose the relevant check (use the same executable path):

```sh
systemctl --user is-enabled docket-opencode-start.service
systemctl --user status docket-opencode-start.service
journalctl --user -u docket-opencode-start.service -n 40 --no-pager
/path/to/installed/opencode service status
```

`active (exited)` is expected for this oneshot after successful startup; it is not
continuous OpenCode health monitoring. Lifecycle commands below are individual
operator choices, **not** a script to run sequentially:

```sh
systemctl --user start docket-opencode-start.service
systemctl --user disable docket-opencode-start.service
systemctl --user stop docket-opencode-start.service
```

Disabling only prevents later automatic startup; it does not stop the active unit.
Stopping an active oneshot may terminate the server it launched through its cgroup,
even without `ExecStop`. Choose a quiet time before stopping/removing the active
unit or stopping the OpenCode server; agent sessions may be interrupted. To remove
this companion, disable it, stop it at that quiet time, remove only its unit file
from the chosen user-unit directory, then run `systemctl --user daemon-reload`.
Docket's own stop/uninstall is separate and manages neither this companion nor
the OpenCode service.

## Windows: interactive-logon scheduled task

Use ordinary **non-elevated** PowerShell as the owning interactive user. This is
a per-user `InteractiveToken` / `LeastPrivilege` logon task, not a Windows boot
service, SYSTEM task, password/S4U task or logged-out daemon. A permitted interactive
logon must exist. If registration or local metadata access is denied, stop; do
not elevate, change policy, install management modules or weaken ACLs.

The example selects a pre-provisioned dedicated venv. **Manual service CLI calls
must use the manifest runtime's console sibling `python.exe`**, from its source
root. Retain `pythonw.exe` as the scheduled daemon interpreter. Do not substitute
base Python or invoke the private task launcher directly.

```powershell
$env:DOCKET_SERVICE_ROOT = Join-Path $env:LOCALAPPDATA 'DocketApp'
$runtime = Join-Path $env:LOCALAPPDATA 'DocketRuntime'
$env:DOCKET_SERVICE_PYTHON = Join-Path $runtime 'Scripts\pythonw.exe'
$console = Join-Path $runtime 'Scripts\python.exe'
$env:DOCKET_HOME = Join-Path $env:LOCALAPPDATA 'Docket'
Set-Location -LiteralPath $env:DOCKET_SERVICE_ROOT
& $console -B -m docket service install
& $console -B -m docket service status
& $console -B -m docket service start
& $console -B -m docket service stop
& $console -B -m docket service restart
& $console -B -m docket service uninstall
```

Choose actions individually and only when authorized. For an existing install,
use its recorded root/runtime/state, not newly chosen example paths. Local drive
paths must be absolute and unambiguous; UNC/device, temporary, reparse and known
worktree paths are refused.

- Install registers the exact owned per-user task; it does **not** start it.
  Start is explicit for the current session; a later interactive logon is the
  configured trigger. Task naming is derived from the current user, not an
  operator-supplied target account.
- Start checks a corroborated existing runtime and readiness without launching
  another; a stopped start requests one launch and requires new-runtime readiness.
- Stop checks ownership, stops only the guarded task, waits for captured runtime
  identities to exit and for a fresh Ready state. It leaves registration for
  subsequent logon/manual start. Scheduler termination is not graceful POSIX shutdown.
- Restart additionally requests one new launch after those barriers and checks
  new-runtime readiness. A timeout/refusal is not success and must not trigger an
  automatic client retry loop or an unguarded native task command.
- Uninstall uses the stop/exit barrier before deleting only the exact owned task.
  It retains **all state, token, pairing, history, logs, private
  `windows-service.json` manifest, source and interpreter**.
- Status displays task presence, sampled Scheduler State (2 queued, 3 ready,
  4 running) and last result. State 4, task presence, or a zero CLI exit alone is
  not daemon health, proof of replacement, or proof that no orphan exists.

The backend guards the full task definition, current owner, private manifest,
deployment digest and protected task permissions before control. A refusal is
not an invitation to take over or repair an unknown task. Logs are private append
files under the selected state root:

```powershell
Get-Content -LiteralPath (Join-Path $env:DOCKET_HOME 'daemon.out.log') -Tail 40
Get-Content -LiteralPath (Join-Path $env:DOCKET_HOME 'daemon.err.log') -Tail 40
```

**Native evidence (2026-10-21):** install/start/stop, health and one supported
Docket daemon restart have evidence. With official OpenCode CLI 2.1.2 and native
OpenAI OAuth, real Docket timers, recurring schedules, all five watches and late
wakes have saved native intake/ACK evidence; a target invalidated before fire
has undeliverable evidence. An owned-daemon `process_ended` identity survived that
restart and produced one consumed wake/ACK, without a source patch. Process
identity must be visible to the daemon; unavailable identity must fail closed,
not count as exit. This does not promise arbitrary process readability or diagnose
permissions from a separate reader's success. One supported old-source uninstall/
new-source installation preserved state. Actual Windows logon automatically
started both services, and the original retained watch produced one consumed
wake with exact saved native intake and a completed tool-free ACK. OS reboot was
not separately tested. Source/mock coverage is not native proof or permission to
run native validation.

### Separate OpenCode per-user logon-start dependency

Normal native agent availability requires the official shared OpenCode service
as well as Docket. Docket service install/start/stop/uninstall **does not create
or manage** this dependency or its startup entry. Official CLI 2.1.2 service help
exposes no documented logon-enable facility. CLI autostart on use alone is not
logon-start proof. If another manager already starts the same service, do not
add a duplicate startup entry.

An operator can separately provision one clearly owned Task Scheduler entry,
`Docket-OpenCode-start-<user>`. Use the ordinary Scheduler UI/COM interfaces as
the current non-elevated user, with this field contract:

- Use an exclusively owned, previously absent name. COM registration uses
  create-not-overwrite flags `0x12` (`CREATE | DONT_ADD_PRINCIPAL_ACE`). Never
  replace or adopt an unknown existing task; stop on refusal rather than relax
  guards or repair permissions.
- Current-user owner and principal; `InteractiveToken`, `LeastPrivilege`, no
  credentials or task password. Exactly one enabled logon trigger for that same
  user, not a boot trigger or another account.
- Protected task permissions granting full access only to the current user and
  SYSTEM. Verify owner, permissions and the complete definition through ordinary
  readback; do not assume the UI's default ACL matches this contract.
- Exactly one Exec action using genuine system Windows PowerShell 5.1, with the
  fixed arguments below invoking only the reviewed absolute official
  `opencode.cmd service start`. Use an existing working directory owned by the
  same user. No helper file, wrapper, execution-policy bypass or Docket setting.
- Enabled, allow demand start, `IgnoreNew`, start when available, allow battery
  start and do not stop on battery; finite two-minute execution limit. No idle
  or network gate, wake-to-run, repetition or restart loop. Keep the task visible;
  only its action window is hidden.

Example action fields (fictitious launcher and working-directory paths; replace
with reviewed persistent installed locations, not aliases or temporary files):

```text
Program: C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe
Arguments: -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -Command "& 'C:\ExampleTools\OpenCode\opencode.cmd' service start > $null; exit $LASTEXITCODE"
Start in: C:\Users\ExampleUser\Applications\Docket
```

This starts the official shared service, not a second Docket daemon or a
supervisor. Readiness and delivery still need independent evidence.

#### Inspect, disable, enable or remove only the owned entry

Select the explicit known-owned task name first. `ExampleUser` below is a
placeholder; replace it consistently with that selected name. These built-in
`schtasks.exe` commands are **individual operator choices**, not a sequence:

```bat
schtasks.exe /Query /TN "\Docket-OpenCode-start-ExampleUser" /V /FO LIST
schtasks.exe /Change /TN "\Docket-OpenCode-start-ExampleUser" /Disable
schtasks.exe /Change /TN "\Docket-OpenCode-start-ExampleUser" /Enable
schtasks.exe /End /TN "\Docket-OpenCode-start-ExampleUser"
schtasks.exe /Delete /TN "\Docket-OpenCode-start-ExampleUser"
```

Query and confirm ownership/definition before any change or deletion; stop if
unclear. Do not use task wildcards, force overwrite or general cleanup. Disable
prevents the next trigger only; it does not stop a running action or server.
Enable permits future triggers, not an immediate service start. End terminates
only a currently running task action, not the detached OpenCode service. Delete
removes only this startup entry: it does **not** stop or uninstall OpenCode or
Docket, or erase their state. Official `opencode service stop` is a separate
shared-server operation requiring operator quiet time and coordination with
other work; never stop it blindly as part of startup-entry removal.

#### Current native qualification

One such entry passed registration, security/definition readback and a manual
idempotent Run before the principal-reported signout/signin. After that actual
logon, both tasks had advanced run times and both services had new process
identities. Direct health, normal discovery and HTTP checks established automatic
availability **before any CLI call that could autostart OpenCode**, without
manually starting either service.

On those same unchanged automatically started services, one exclusive write to
the original retained watch's absent target caused one occurrence, one delivery
attempt and a consumed receipt. The frozen request matched the saved native user
message and full installed formatter byte-for-byte, followed by the sole exact
completed tool-free ACK. Final pending/outstanding counts were zero, and all prior
item metadata was retained unchanged. No restart, deployment, repair, reconcile,
resend or replacement intent was needed.

This bounded Windows logon and retained-watch proof used the unchanged installed
code, not the later source-only normal-waiting status correction. That correction
and its mock regressions are separate from native proof. OS reboot was not
separately tested; neither this result nor task result zero promises universal
availability, uptime or arbitrary task execution.

## Foreground operation and maintenance

On Unix, `bin/docket serve` from the source root runs in the foreground; on
Windows use the same console runtime and root above with `& $console -B -m docket serve`.
Ctrl-C stops foreground operation. Do not run it alongside a registered daemon:
they share state and a default loopback port (`127.0.0.1:8768`). `GET /health`
indicates HTTP service availability, not agent delivery/consumption.

Uninstall is not data erasure. Before any reviewed migration, preserve a private,
consistent SQLite backup including WAL effects, integrity and item invariants,
plus the own-client binding and service record. Never copy an old DB over a live
database, rotate caller/request identity, or let an old writer retry a database
using the newer immutable-request format. Restoring can discard receipts or
replay stale work. Recovery needs explicit coordination and a stopped Docket
service; see [delivery status](delivery-status.md).
