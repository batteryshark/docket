# OpenChamber client pairing

Pairing makes Docket its own named desktop client with broad session API access, not a wake-only capability. It stores only its own returned token and binding. The token has no pairing-imposed expiry; manage the client through OpenChamber's device list. Generic OpenCode installations can operate without pairing; see [host setup](docs/host-setup.md).

## Pair

1. In a private local terminal, run `docket pair --stdin` (or `bin/docket pair --stdin` from the checkout).
2. In OpenChamber, open Settings → Remote instances → Connect to this server → Add a device. Choose a display name, select **This computer only**, create the QR code and copy **Connection link**.
3. Paste the link into Docket's hidden terminal prompt within ten minutes. Do not put it in command arguments, shell history, chat or a plaintext file.

The CLI checks `/health`, then performs one redeem POST to the identified numeric loopback origin. It refuses redirects, non-loopback candidates and `localhost` name resolution. Enrollment does not restart or install a service. Display names are not credential identity.

## Private state

`$DOCKET_HOME/openchamber` (default `~/.docket/openchamber`) must be owned by the user, mode 0700 and not a symlink. Private files are mode 0600. Reads reject symlinks, hardlinks, foreign ownership and oversized files.

- `client.json`: own token, client ID, trusted origin, version and stable deduplication key.
- `identity.json`: stable `docket:<uuid>` identity.
- `attempt.json`: canonical enrollment digests, never the connection link or one-use secret.

Docket atomically saves the returned client ID and token before checking optional response metadata, so an unfamiliar display field cannot discard a newly minted credential. Missing metadata produces sanitized warnings. Names and branding never select another origin.

Concurrent enrollment is refused using an exclusive `.enrolling` lock. After a timeout, lost response, malformed response or failed write, do not retry the same link: the host may have created the client already. Inspect the device list and resolve that enrollment before trying a new one. Existing bindings are not automatically replaced; there is no automated reset command. Enrollment attempts conservatively lock host selection even when enrollment fails.

## Delivery and status

Pairing state takes precedence over `DOCKET_DELIVERY_HOST`, including `opencode` and unpaired modes. Missing, unreadable, expired, revoked or mismatched bindings fail visibly without falling back to another credential or anonymous route.

For desktop requests Docket reads the numeric `desktopLocalPort` from `~/.config/openchamber/settings.json`, verifies `/health` and sends its bearer only to that loopback origin. Redirects and environment HTTP proxies are disabled. OpenChamber supplies the managed OpenCode connection.

Before each paired session request, `/api/client-auth/clients` must return exactly one matching own client record without expiry or revocation. This is a local own-record guard, not a host-enforced wake-only capability. Optional-auth hosts may accept missing or bogus bearers, and a guard/request race remains; live revocation behavior is not established.

`/health` and `/api/v1/meta` expose sanitized host mode, origin and last-check information, never tokens. New schedules and receipts record the observed host binding. See [delivery status](docs/delivery-status.md) for admission, saved intake, reconciliation and cancellation semantics, and [services](docs/services.md) for maintenance and consistent backups.
