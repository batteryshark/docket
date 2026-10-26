# Delivery host setup

Docket's default adapter discovers a local OpenCode URL from `~/.local/state/opencode/service.json`, falling back to `opencode service status`. The runtime password comes from that state file or `~/.config/opencode/service.json`; Basic authentication uses username `opencode`. The URL must be loopback. Keep these files private.

The host must support `POST /api/session/{session_id}/prompt` with a caller-supplied stable `msg_` ID, `text`, `delivery` (`queue` or `steer`) and `resume: true`. Repeated IDs must preserve the original admission without duplication. Reconciliation uses `GET /api/session/{session_id}/message/{id}` and `GET /api/session/{session_id}/inbox`; cancellation uses `DELETE /api/session/{session_id}/inbox/{id}`. Message lookup must distinguish the exact saved user message from a queued admission.

Normal wakes queue for the end of the current turn. Urgent wakes use steer at a model-step boundary, not during token generation. The target host must own the session's active turn. A shared transcript database alone does not establish that compatibility.

Optional [OpenChamber pairing](../PAIRING.md) gives Docket its own desktop-client identity and broad session API access. Pairing state locks host selection: a missing, expired, revoked or mismatched binding does not fall back to another credential or unauthenticated route. The own-record check is not a host-enforced wake-only capability, and a check/request race remains.

Use `--directory` to pin an OpenCode target to its location. Missing, archived or wrong-location sessions are refused or become undeliverable. Pi targets use a separate explicit adapter; see [Pi setup](pi.md).

Docket does not start the agent host. Service startup and private-state maintenance are covered in [Services](services.md); delivery uncertainty and recovery are covered in [Delivery status](delivery-status.md).
