"""Small public delivery contract. No payloads or provider diagnostics escape here."""

from __future__ import annotations

import json
import re

RECEIPT_STATES = frozenset(("prepared", "sending", "admitted", "consumed", "ambiguous",
                            "lost", "failed", "missing", "retracted"))

MESSAGES = {
    "provider_failed": "The provider reported failure before claim; this request will not be resent.",
    "unavailable": "The delivery provider could not be reached or checked.",
    "bad_target": "The original target session was not found.",
    "archived": "The original target session is archived.",
    "wrong_target": "The target is no longer in the original directory.",
    "refused": "The configured host refused Docket's client; no fallback was used.",
    "conflict": "The provider reported an identity conflict; this request will not be resent.",
    "acceptance_unknown": "An earlier send may have been accepted. Only status checks are safe.",
    "lost": "The accepted request is no longer visible; consumption is uncertain.",
    "legacy_envelope_unknown": "The original complete request was not saved; resend is unsafe.",
    "legacy_identity_unknown": "An earlier attempt has no saved request identity; resend is unsafe.",
    "backend_unverifiable": "The original provider namespace cannot be verified for recovery.",
    "backend_changed": "The prepared request belongs to a different provider binding.",
    "already_consumed": "The session already consumed this request; execution is not established.",
    "already_admitted": "The provider already accepted this request. Check status instead.",
    "cancelled_identity": "This request was stopped and cannot be retried.",
    "in_flight": "A delivery operation for this request is still running.",
    "schedule_advanced": "Only the currently open occurrence can be retried.",
    "schedule_finished": "A finished schedule cannot be restarted by recovery.",
    "retry_budget": "The one explicit recovery authorization has already been used.",
    "attempt_budget": "The bounded automatic attempt budget was exhausted before sending.",
    "limits": "Recovery would exceed an existing scheduling limit.",
    "not_ready": "This request is not eligible for explicit recovery.",
    "invalid": "Recovery requires an empty JSON object and an exact existing unit.",
    "unsupported_capability": "The provider does not support this operation.",
    "invalid_receipt": "Saved receipt metadata is malformed or unsupported; affected fields are unknown.",
    "legacy_identity_unverified": "Legacy request identity is unverified for display; its stored value is retained for status-only reconciliation.",
}


def issue(code, *, phase="validation", certainty="not_admitted"):
    code = code if code in MESSAGES else "unavailable"
    return {"code": code, "detail": MESSAGES[code], "phase": phase,
            "admission_certainty": certainty}


def decode_receipt(raw: object) -> tuple[object, bool]:
    """Decode legacy storage without throwing or retaining a malformed JSON body."""
    if raw is None:
        return None, False
    try:
        value = json.loads(raw)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None, True
    return value, not isinstance(value, dict)


def admission_id(value: object) -> str | None:
    """Local display policy, NOT OpenCode's accepted-ID grammar or maximum.

    Only known Docket-generated msg_32hex IDs have approved display evidence.
    Other stored legacy strings remain private and are not replaced or reminted.
    """
    return value if isinstance(value, str) and re.fullmatch(r"msg_[0-9a-f]{32}", value) else None


def receipt_projection(value: object, *, malformed: bool = False) -> dict:
    """One typed compatibility boundary, not a redactor or a source of core facts."""
    bad = malformed or (value is not None and not isinstance(value, dict))
    source = value if isinstance(value, dict) else {}
    result = {"state": None, "admission_id": None, "inbox_id": None, "delivery": None}
    unverified = False
    for key, allowed in (("state", RECEIPT_STATES), ("delivery", ("queue", "steer"))):
        if key in source:
            field = source[key]
            if isinstance(field, str) and field in allowed:
                result[key] = field
            else:
                bad = True
    for key in ("admission_id", "inbox_id"):
        if key in source:
            result[key] = admission_id(source[key])
            if result[key] is None:
                if isinstance(source[key], str):
                    unverified = True
                else:
                    bad = True
    if source and not any(key in source for key in result):
        bad = True
    code = "invalid_receipt" if bad else "legacy_identity_unverified" if unverified else None
    result["issue"] = issue(code, phase="projection", certainty="unknown") if code else None
    return result


def retry_refusal(request):
    if not request:
        return "not_ready"
    facts = request["facts"]
    if request["stop_requested"] or facts.get("cancelled"):
        return "cancelled_identity"
    if facts.get("consumed"):
        return "already_consumed"
    if facts.get("admitted") or facts.get("claimed") or facts.get("injected"):
        return "already_admitted"
    if request["provenance"] == "legacy":
        return "legacy_envelope_unknown" if request["request_id"] else "legacy_identity_unknown"
    if request["dispatch_state"] == "possible":
        return "acceptance_unknown"
    frozen = request["frozen"]
    if frozen and not (frozen["binding"].get("namespace_verified") or request.get("binding_owned")):
        return "backend_unverifiable"
    if request["manual_used"] and not request["manual_pending"]:
        return "retry_budget"
    return None


def project(store, item, occ=None):
    """The only delivery projection, shared by all HTTP views and mutations."""
    unit = occ or item
    request = store.get_request(unit.id)
    receipt = receipt_projection(unit.receipt, malformed=unit.receipt_malformed)
    facts = dict(request["facts"]) if request else {}
    legacy = request is None or request["provenance"] == "legacy"
    # Historical proof is preserved, but explicitly marked as legacy evidence.
    if not request and (receipt.get("state") == "consumed" or unit.status == "delivered"):
        facts["consumed"] = True
    elif not request and receipt.get("state") == "admitted":
        facts["admitted"] = True
    diagnostic = request["issue"] if request else None
    if not request and unit.error:
        code = unit.error.partition(":")[0]
        diagnostic = issue(code, certainty="unknown")
    missing_occurrence = (occ is None and item.kind in ("recurring", "watch")
                          and ((item.spec or {}).get("open_occurrence_id")
                               or item.id in store.open_summaries()))
    if missing_occurrence:
        diagnostic = issue("legacy_identity_unknown", phase="projection", certainty="unknown")
        outcome = "uncertain"
    elif item.kind == "recurring" and occ is None:
        outcome = "not_applicable"
    elif facts.get("consumed"):
        outcome = "delivered"
    elif facts.get("cancelled"):
        outcome = "cancelled"
    elif facts.get("failed_before_claim") and not (facts.get("claimed") or facts.get("injected")):
        outcome = "failed"
    elif request and request["dispatch_state"] == "possible" and not facts.get("admitted"):
        outcome = "uncertain"
    elif diagnostic and diagnostic["code"] in ("lost", "acceptance_unknown"):
        outcome = "uncertain"
    elif facts.get("admitted"):
        outcome = "admitted"
    elif legacy and (unit.attempts or unit.status in ("firing", "ambiguous", "undeliverable")):
        outcome = "uncertain"
    elif unit.status == "cancelled":
        outcome = "cancelled" if request and request["dispatch_state"] != "possible" else "uncertain"
    elif unit.status == "undeliverable":
        outcome = "failed"
    elif unit.status == "ambiguous":
        outcome = "uncertain"
    else:
        outcome = "pending"
    refusal = retry_refusal(request)
    if item.status == "cancelled":
        refusal = "cancelled_identity"
    elif item.kind in ("watch", "recurring"):
        if not occ:
            refusal = "schedule_finished" if item.status not in ("pending", "firing") else "not_ready"
        elif (item.spec or {}).get("open_occurrence_id") != occ.id:
            refusal = "schedule_advanced"
    if not refusal and unit.status not in ("undeliverable", "ambiguous", "pending"):
        refusal = "not_ready"
    if not refusal and not unit.attempts:
        refusal = "not_ready"
    request_id = admission_id(request["request_id"]) if request else receipt["admission_id"] or receipt["inbox_id"]
    receipt_issue = receipt["issue"]
    if request and request["request_id"] is not None and request_id is None:
        code = "legacy_identity_unverified" if isinstance(request["request_id"], str) else "invalid_receipt"
        receipt_issue = receipt_issue or issue(code, phase="projection", certainty="unknown")
    # Hidden legacy strings can still be reconciled by exact unit; display policy
    # must never replace private identity or expand retry eligibility.
    private_id = request["request_id"] if request else (
        (unit.receipt.get("admission_id") or unit.receipt.get("inbox_id")) if isinstance(unit.receipt, dict) else None)
    return {
        "unit_id": unit.id, "request_id": request_id,
        "outcome": outcome, "phase": (request or {}).get("phase", "unknown"),
        "facts": {key: facts.get(key) for key in ("admitted", "claimed", "injected", "consumed", "cancelled")},
        "execution": "unknown", "issue": diagnostic, "evidence": "legacy" if legacy else "request",
        "last_checked_at": (request or {}).get("checked_at"),
        "stale": bool((request or {}).get("check_issue")),
        "check_issue": (request or {}).get("check_issue"),
        "receipt_issue": receipt_issue,
        "namespace_verified": bool(((request or {}).get("frozen") or {}).get("binding", {}).get("namespace_verified")),
        "same_daemon_recovery": bool((request or {}).get("binding_owned")
                                      and request["dispatch_state"] == "never"),
        "recovery": {"reconcile": isinstance(private_id, str) and bool(private_id),
                     "retry": refusal is None, "refusal_code": refusal},
    }
