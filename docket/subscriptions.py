"""Durable subscription intake, enabled only by explicit daemon configuration.

Installed configuration is trusted operator input. Events are bounded data only.
Shares Store's SQLite/lock/private-file discipline, but never creates an Item or
sends to a host. 'prepared' means a durable candidate briefing, NOT permission to
execute: host owner/stop fencing and execution-time narrowing remain mandatory.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets

from docket.parse import new_admission_id
from docket.store import Store


SCHEMA = """
CREATE TABLE IF NOT EXISTS subscription_publishers (
  id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS event_subscriptions (
  id TEXT PRIMARY KEY, publisher_id TEXT NOT NULL, event_type TEXT NOT NULL,
  config_json TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
  UNIQUE(publisher_id, event_type)
);
CREATE TABLE IF NOT EXISTS subscription_events (
  id TEXT PRIMARY KEY, publisher_id TEXT NOT NULL, external_id TEXT NOT NULL,
  payload_hash TEXT NOT NULL, event_json TEXT NOT NULL, accepted_at TEXT NOT NULL,
  UNIQUE(publisher_id, external_id)
);
CREATE TABLE IF NOT EXISTS subscription_deliveries (
  id TEXT PRIMARY KEY, event_id TEXT NOT NULL, subscription_id TEXT NOT NULL,
  admission_id TEXT UNIQUE NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('accepted', 'prepared', 'admitted')),
  config_json TEXT NOT NULL, prepared_json TEXT, prepared_at TEXT,
  UNIQUE(event_id, subscription_id)
);
CREATE TABLE IF NOT EXISTS subscription_installation (
  slot INTEGER PRIMARY KEY CHECK(slot=1), manifest_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscription_rate_windows (
 scope TEXT PRIMARY KEY, window INTEGER NOT NULL, count INTEGER NOT NULL
);
"""
RESERVED = {"model", "role", "session", "session_id", "profile", "permissions",
            "tools", "system", "owner", "source", "publisher", "token", "budget"}
LEVELS = {"deny": 0, "review": 1, "allow": 2}


class SubscriptionRefusal(ValueError):
    """Messages contain no raw token or event body."""

    def __init__(self, message, *, code="invalid"):
        super().__init__(message)
        self.code = code


def _text(value, limit=128):
    try:
        valid = isinstance(value, str) and value.strip() and len(value.encode()) <= limit
    except UnicodeError:
        valid = False
    if not valid:
        raise SubscriptionRefusal("invalid bounded string")
    return value


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _policy(policy):
    if not isinstance(policy, dict) or len(policy) > 64:
        raise SubscriptionRefusal("invalid installed policy")
    for tool, effect in policy.items():
        _text(tool)
        if not isinstance(effect, str) or effect not in LEVELS:
            raise SubscriptionRefusal("invalid installed effect")


def _config(config):
    if isinstance(config, dict) and set(config) == {"action", "baseline", "profile"}:
        if config["action"] not in ("owner", "handler") or not all(isinstance(config[k], dict) and config[k] for k in ("baseline", "profile")):
            raise SubscriptionRefusal("invalid native installed configuration")
        fields = config["profile"].get("fields")
        if not isinstance(fields, list) or len(fields) > 32:
            raise SubscriptionRefusal("invalid native data fields")
        for field in fields:
            _text(field)
            if field.casefold() in RESERVED:
                raise SubscriptionRefusal("authority fields cannot be event data fields")
        if len(fields) != len(set(fields)):
            raise SubscriptionRefusal("duplicate native data field")
        # Native profile validator owns this versioned schema and rejects
        # widening. Installation itself never authorizes its execution.
        if len(_json(config).encode()) > 49152:
            raise SubscriptionRefusal("native configuration too large")
        return json.loads(_json(config))
    keys = {"profile_id", "profile_version", "baseline_version", "owner_generation",
            "target", "model", "role", "template", "fields", "required_tools",
            "baseline_policy", "subscription_policy", "profile_policy",
            "baseline_budget", "subscription_budget", "profile_budget"}
    if not isinstance(config, dict) or set(config) - {"action"} != keys:
        raise SubscriptionRefusal("invalid installed configuration fields")
    if config.get("action", "owner") not in ("owner", "handler"):
        raise SubscriptionRefusal("invalid installed action")
    for key in ("profile_id", "profile_version", "baseline_version", "owner_generation", "model", "role"):
        _text(config[key])
    _text(config["template"], 4000)
    target = config["target"]
    if (not isinstance(target, dict) or set(target) != {"session_id", "directory"}
            or not _text(target["session_id"]).startswith("ses_")
            or not Path(_text(target["directory"], 1024)).is_absolute()):
        raise SubscriptionRefusal("invalid installed owner target")
    for key in ("fields", "required_tools"):
        values = config[key]
        if not isinstance(values, list) or len(values) > 32:
            raise SubscriptionRefusal("invalid installed field/tool list")
        for value in values:
            _text(value)
        if len(values) != len(set(values)):
            raise SubscriptionRefusal("duplicate installed field/tool")
    if any(field.casefold() in RESERVED for field in config["fields"]):
        raise SubscriptionRefusal("authority fields cannot be event data fields")
    for key in ("baseline_policy", "subscription_policy", "profile_policy"):
        _policy(config[key])
    for key in ("baseline_budget", "subscription_budget", "profile_budget"):
        if type(config[key]) is not int or not 1 <= config[key] <= 1_000_000:
            raise SubscriptionRefusal("invalid installed budget")
    return json.loads(_json(config))  # Detach from caller's mutable configuration.


class SubscriptionStore(Store):
    """Explicit opt-in store. Trusted install/revoke calls have no HTTP route."""

    def __init__(self, path: Path):
        super().__init__(path)
        with self._lock:
            self._db.executescript(SCHEMA)
            self._commit()

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._commit()
            except BaseException:
                self._db.rollback()
                raise

    def install_publisher(self, publisher_id: str, token: str):
        _text(publisher_id)
        _text(token, 512)
        if len(token) < 32:
            raise SubscriptionRefusal("publisher credential too short")
        with self._transaction():
            # Immutable identity: no replace/implicit reactivation of revoked publishers.
            self._db.execute("INSERT INTO subscription_publishers(id, token_hash) VALUES (?,?)",
                             (publisher_id, _hash(token)))

    def install_subscription(self, subscription_id: str, publisher_id: str,
                             event_type: str, config: dict):
        for value in (subscription_id, publisher_id, event_type):
            _text(value)
        config = _config(config)
        with self._transaction():
            if not self._db.execute("SELECT 1 FROM subscription_publishers WHERE id=? AND revoked=0",
                                    (publisher_id,)).fetchone():
                raise SubscriptionRefusal("publisher unavailable")
            self._db.execute("INSERT INTO event_subscriptions(id,publisher_id,event_type,config_json) VALUES (?,?,?,?)",
                             (subscription_id, publisher_id, event_type, _json(config)))

    def revoke(self, *, publisher_id=None, subscription_id=None):
        if (publisher_id is None) == (subscription_id is None):
            raise SubscriptionRefusal("choose one installed revocation target")
        table = "subscription_publishers" if publisher_id is not None else "event_subscriptions"
        identity = _text(publisher_id if publisher_id is not None else subscription_id)
        with self._transaction():
            self._db.execute(f"UPDATE {table} SET revoked=1 WHERE id=?", (identity,))

    def install_manifest(self, manifest: dict):
        """Trusted offline installation, atomic/idempotent; no revocation reset.

        Caller must validate file provenance separately. Version replacement is
        deliberately refused until an explicit audited upgrade contract exists.
        """
        if (not isinstance(manifest, dict) or set(manifest) != {"version", "publisher", "subscription"}
                or type(manifest["version"]) is not int or manifest["version"] not in (1, 2)):
            raise SubscriptionRefusal("unsupported installed manifest")
        publisher, sub = manifest["publisher"], manifest["subscription"]
        if not isinstance(publisher, dict) or set(publisher) != {"id", "token_sha256"}:
            raise SubscriptionRefusal("invalid installed publisher")
        _text(publisher["id"])
        hashed = _text(publisher["token_sha256"], 64)
        if len(hashed) != 64 or any(c not in "0123456789abcdef" for c in hashed):
            raise SubscriptionRefusal("invalid publisher credential hash")
        if not isinstance(sub, dict) or set(sub) != {"id", "event_type", "profile"}:
            raise SubscriptionRefusal("invalid installed subscription")
        _text(sub["id"])
        _text(sub["event_type"])
        profile = _config(sub["profile"])
        if (manifest["version"] == 2) != (set(profile) == {"action", "baseline", "profile"}):
            raise SubscriptionRefusal("manifest/profile schema version mismatch")
        encoded = _json(manifest)
        with self._transaction():
            old = self._db.execute("SELECT manifest_json FROM subscription_installation WHERE slot=1").fetchone()
            if old:
                if old[0] != encoded:
                    raise SubscriptionRefusal("installed manifest replacement requires explicit upgrade", code="conflict")
                return
            # No implicit adoption/overwriting of an independently configured candidate.
            if self._db.execute("SELECT 1 FROM subscription_publishers UNION ALL SELECT 1 FROM event_subscriptions LIMIT 1").fetchone():
                raise SubscriptionRefusal("existing configuration requires explicit adoption", code="conflict")
            self._db.execute("INSERT INTO subscription_publishers(id,token_hash) VALUES (?,?)",
                             (publisher["id"], hashed))
            self._db.execute("INSERT INTO event_subscriptions(id,publisher_id,event_type,config_json) VALUES (?,?,?,?)",
                             (sub["id"], publisher["id"], sub["event_type"], _json(profile)))
            self._db.execute("INSERT INTO subscription_installation VALUES (1,?)", (encoded,))

    def configured(self):
        with self._lock:
            return self._db.execute("SELECT 1 FROM subscription_installation WHERE slot=1").fetchone() is not None

    def authenticate(self, token: str):
        """Pre-body authentication; accept() repeats it inside the write transaction."""
        _text(token, 512)
        with self._lock:
            row = self._db.execute("SELECT id FROM subscription_publishers WHERE token_hash=? AND revoked=0",
                                   (_hash(token),)).fetchone()
            if not row:
                raise SubscriptionRefusal("publisher authentication refused", code="unauthorized")
            return row["id"]

    def rate_request(self, publisher_id, *, now=None):
        """Bound authenticated requests (including invalid bodies and duplicates)."""
        now = datetime.now(timezone.utc).timestamp() if now is None else now
        with self._transaction():
            self._rate("publisher:" + publisher_id, int(now // 60), 120)

    def _rate(self, scope, window, limit):
        row = self._db.execute("SELECT * FROM subscription_rate_windows WHERE scope=?", (scope,)).fetchone()
        count = row["count"] if row and row["window"] == window else 0
        if count >= limit:
            raise SubscriptionRefusal("event rate limit reached", code="capacity")
        self._db.execute("INSERT OR REPLACE INTO subscription_rate_windows VALUES (?,?,?)", (scope, window, count + 1))

    def accept(self, token: str, event: dict) -> dict:
        """Authenticate, authorize and atomically persist event + one delivery.

        No event-selected profile or target. One installed subscription per
        publisher/type in this bounded candidate; no retroactive fanout on retry.
        """
        _text(token, 512)
        if not isinstance(event, dict) or set(event) != {"id", "type", "payload"}:
            raise SubscriptionRefusal("invalid event envelope fields")
        for key in ("id", "type"):
            _text(event[key])
        if not isinstance(event["payload"], dict):
            raise SubscriptionRefusal("invalid payload")
        try:
            encoded = _json(event)
            encoded_size = len(encoded.encode())
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise SubscriptionRefusal("invalid event data") from None
        if encoded_size > 16384:
            raise SubscriptionRefusal("event too large")
        event = json.loads(encoded)
        with self._transaction():
            publisher = self._db.execute("SELECT id FROM subscription_publishers WHERE token_hash=? AND revoked=0",
                                         (_hash(token),)).fetchone()
            if not publisher:
                raise SubscriptionRefusal("publisher authentication refused", code="unauthorized")
            sub = self._db.execute("SELECT * FROM event_subscriptions WHERE publisher_id=? AND event_type=? AND revoked=0",
                                   (publisher["id"], event["type"])).fetchone()
            if not sub:
                raise SubscriptionRefusal("subscription unavailable", code="forbidden")
            config = json.loads(sub["config_json"])
            fields = config.get("fields")
            if fields is None:
                fields = config["profile"].get("fields")
            if not isinstance(fields, list) or set(event["payload"]) != set(fields):
                raise SubscriptionRefusal("payload fields do not match installed profile")
            for value in event["payload"].values():
                _text(value, 8000)  # No nested event-supplied configuration.
            old = self._db.execute("SELECT * FROM subscription_events WHERE publisher_id=? AND external_id=?",
                                   (publisher["id"], event["id"])).fetchone()
            if old:
                if old["payload_hash"] != _hash(encoded):
                    raise SubscriptionRefusal("event identity payload conflict", code="conflict")
                row = self._db.execute("SELECT * FROM subscription_deliveries WHERE event_id=?", (old["id"],)).fetchone()
                return self._receipt(row)
            outstanding = self._db.execute("SELECT COUNT(*) FROM subscription_deliveries d JOIN subscription_events e ON e.id=d.event_id WHERE e.publisher_id=?",
                                            (publisher["id"],)).fetchone()[0]
            if outstanding >= 100:
                raise SubscriptionRefusal("candidate publisher intake capacity reached", code="capacity")
            count = self._db.execute("SELECT COUNT(*) FROM subscription_deliveries WHERE subscription_id=?", (sub["id"],)).fetchone()[0]
            if count >= 100:
                raise SubscriptionRefusal("subscription capacity reached", code="capacity")
            self._rate("subscription:" + sub["id"], int(datetime.now(timezone.utc).timestamp() // 60), 120)
            event_id, delivery_id = "evt_" + secrets.token_hex(16), "evd_" + secrets.token_hex(16)
            self._db.execute("INSERT INTO subscription_events VALUES (?,?,?,?,?,?)",
                             (event_id, publisher["id"], event["id"], _hash(encoded), encoded,
                              datetime.now(timezone.utc).isoformat()))
            self._db.execute("INSERT INTO subscription_deliveries(id,event_id,subscription_id,admission_id,status,config_json) VALUES (?,?,?,?,?,?)",
                             (delivery_id, event_id, sub["id"], new_admission_id(), "accepted", sub["config_json"]))
            return self._receipt(self._db.execute("SELECT * FROM subscription_deliveries WHERE id=?", (delivery_id,)).fetchone())

    @staticmethod
    def _receipt(row):
        return {key: row[key] for key in ("id", "event_id", "subscription_id", "admission_id", "status")}

    def prepare(self, delivery_id: str, *, available_tools: set[str]) -> dict:
        """Persist candidate narrowing/data. Does NOT admit a request or verify host stops.

        available_tools is trusted adapter inventory, never a producer field.
        No public admitted/consumed transition until an exact-host-ID adapter exists.
        """
        _text(delivery_id)
        with self._transaction():
            row = self._db.execute("SELECT d.*,e.event_json FROM subscription_deliveries d JOIN subscription_events e ON e.id=d.event_id JOIN event_subscriptions s ON s.id=d.subscription_id JOIN subscription_publishers p ON p.id=s.publisher_id WHERE d.id=? AND s.revoked=0 AND p.revoked=0",
                                   (delivery_id,)).fetchone()
            if not row:
                raise SubscriptionRefusal("delivery unavailable or revoked")
            config = json.loads(row["config_json"])
            if "baseline" in config:
                raise SubscriptionRefusal("native preparation requires the enforced profile host")
            policy = {}
            for tool in config["baseline_policy"]:
                level = min(LEVELS[config[key].get(tool, "deny")] for key in
                            ("baseline_policy", "subscription_policy", "profile_policy"))
                if level and tool in available_tools:
                    policy[tool] = "review" if level == 1 else "allow"
            if any(tool not in policy for tool in config["required_tools"]):
                raise SubscriptionRefusal("required tool unavailable or denied")
            prepared = {"target": config["target"], "owner_generation": config["owner_generation"],
                        "profile_id": config["profile_id"], "profile_version": config["profile_version"],
                        "baseline_version": config["baseline_version"], "model": config["model"],
                        "role": config["role"], "policy": policy,
                        "budget": min(config[key] for key in ("baseline_budget", "subscription_budget", "profile_budget")),
                        "template": config["template"],
                        "event_data": json.loads(row["event_json"])["payload"],
                        "admission_id": row["admission_id"]}
            if row["prepared_json"] is not None:
                if row["prepared_json"] != _json(prepared):
                    raise SubscriptionRefusal("prepared inventory changed; reconciliation required")
                return json.loads(row["prepared_json"])
            self._db.execute("UPDATE subscription_deliveries SET status='prepared',prepared_json=?,prepared_at=? WHERE id=? AND status='accepted'",
                             (_json(prepared), datetime.now(timezone.utc).isoformat(), delivery_id))
            return prepared
