"""Own-record guard. Fake host only; no live pairing or desktop settings."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from docket.delivery import DeliveryError
from docket.host import OpenChamberAdapter, configured_adapter
from docket.pairing import RecordRefusal, atomic_private, require_own_record


def _record(**overrides):
    client = {
        "id": "client-docket",
        "clientKind": "desktop",
        "label": "Docket",
        "deviceName": "Docket",
        "revokedAt": None,
        "expiresAt": None,
    }
    client.update(overrides)
    return client


def _binding():
    return {
        "version": 1,
        "clientId": "client-docket",
        "clientKind": "desktop",
        "label": "Docket",
        "clientToken": "oc_client_testtoken",
    }


class OwnRecordTests(unittest.TestCase):
    def test_requires_exactly_the_stored_active_record(self):
        require_own_record([_record()], _binding())

    def test_renamed_record_and_display_metadata_do_not_change_identity(self):
        for label in ("docket", "DoCkEt", "Phone", "\u00a0Principal's device 🦈\ufeff"):
            with self.subTest(label=label):
                require_own_record([_record(label=label, deviceName="Renamed",
                                            clientKind="mobile")], _binding())
        require_own_record([{"id": "client-docket"}], _binding())

    def test_refuses_revocation_expiry_mismatch_and_extra_records(self):
        binding = _binding()
        cases = (
            [_record(revokedAt="2026-10-16T00:00:00Z")],
            [_record(expiresAt="2026-10-17T00:00:00Z")],
            [_record(id="other")],
            [_record(), _record(id="other", label="Phone", deviceName="Phone")],
            [],
        )
        for clients in cases:
            with self.subTest(clients=clients):
                with self.assertRaises(RecordRefusal) as caught:
                    require_own_record(clients, binding)
                self.assertIn("no fallback", caught.exception.detail)
                self.assertNotIn("oc_client_", caught.exception.detail)


class _Host(ThreadingHTTPServer):
    def __init__(self):
        self.calls = []
        self.clients = [_record()]
        super().__init__(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def do_GET(self):
        self._record()
        if self.path == "/health":
            return self._send(200, {"openchamberVersion": "2.3.3", "status": "ok"})
        if self.path == "/api/client-auth/clients":
            return self._send(200, {"clients": self.server.clients})
        if self.path.startswith("/api/session/"):
            # Optional loopback auth: a missing bearer still reaches the session.
            return self._send(200, {"data": {"id": "ses_known", "directory": "/tmp/proj"}})
        return self._send(404, {"error": "no"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self._record(body)
        if self.path.endswith("/prompt"):
            return self._send(200, {"data": {"id": body.get("id"), "delivery": body.get("delivery")}})
        return self._send(404, {"error": "no"})

    def _record(self, body=None):
        self.server.calls.append({
            "method": self.command,
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": body,
        })

    def _send(self, status, payload):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class AdapterGuardTests(unittest.TestCase):
    def setUp(self):
        self.host = _Host()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "openchamber"
        atomic_private(self.root / "client.json", _binding())
        self.settings = self.root / "settings.json"
        self.settings.write_text(json.dumps({"desktopLocalPort": self.host.server_address[1]}))
        self.adapter = OpenChamberAdapter(root=self.root, settings=self.settings, timeout=2)
        self.adapter._discovery = self._no_fallback

    def tearDown(self):
        self.host.stop()
        self._tmp.cleanup()

    def _no_fallback(self):
        raise AssertionError("fell back to the older OpenCode service")

    def test_optional_loopback_auth_does_not_reject_a_matching_record(self):
        with urllib.request.urlopen(self.host.origin + "/api/session/ses_known", timeout=2) as response:
            anonymous = json.loads(response.read())
        self.assertEqual(anonymous["data"]["id"], "ses_known")
        self.assertIsNone(self.host.calls[-1]["authorization"])
        receipt = self.adapter.deliver(
            {"session_id": "ses_known"}, "wake", urgent=False, admission_id="msg_own",
        )
        self.assertEqual(receipt.admission_id, "msg_own")
        self.assertEqual(self.adapter.host_status()["host_enforced_authentication"], "not proven")
        client_calls = [call for call in self.host.calls if call["path"] == "/api/client-auth/clients"]
        prompt_calls = [call for call in self.host.calls if str(call["path"]).endswith("/prompt")]
        self.assertTrue(client_calls)
        self.assertTrue(all(
            call["authorization"] == "Bearer oc_client_testtoken" for call in client_calls
        ))
        self.assertEqual(len(prompt_calls), 1)
        self.assertEqual(prompt_calls[0]["authorization"], "Bearer oc_client_testtoken")

    def test_revoked_or_extra_record_refuses_visibly_without_fallback(self):
        cases = (
            [_record(revokedAt="2026-10-16T00:00:00Z")],
            [_record(id="other", label="Docket")],
            [_record(), _record(id="other", label="Phone", deviceName="Phone")],
            [_record(expiresAt="2026-10-17T00:00:00Z")],
        )
        for clients in cases:
            with self.subTest(clients=clients):
                self.host.clients = clients
                self.host.calls.clear()
                check = self.adapter.validate({"session_id": "ses_known"})
                self.assertFalse(check.ok)
                self.assertEqual(check.code, "refused")
                self.assertIn("no fallback", check.detail)
                self.assertNotIn("oc_client_", check.detail)
                with self.assertRaises(DeliveryError) as caught:
                    self.adapter.deliver(
                        {"session_id": "ses_known"}, "wake", urgent=False, admission_id="msg_no",
                    )
                self.assertEqual(caught.exception.code, "refused")
                self.assertFalse(caught.exception.retryable)
                self.assertIn("no fallback", caught.exception.detail)
                self.assertFalse(any(
                    str(call["path"]).endswith("/prompt") for call in self.host.calls
                ))
                self.assertEqual(self.adapter.host_status()["state"], "refused")
                self.assertEqual(
                    self.adapter.host_status()["host_enforced_authentication"], "not proven",
                )


class DefaultAdapterTests(unittest.TestCase):
    def test_unpaired_checkout_keeps_the_older_service(self):
        previous = os.environ.get("DOCKET_HOME")
        previous_mode = os.environ.get("DOCKET_DELIVERY_HOST")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["DOCKET_HOME"] = tmp
                os.environ.pop("DOCKET_DELIVERY_HOST", None)
                adapter = configured_adapter()
        finally:
            if previous is None:
                os.environ.pop("DOCKET_HOME", None)
            else:
                os.environ["DOCKET_HOME"] = previous
            if previous_mode is None:
                os.environ.pop("DOCKET_DELIVERY_HOST", None)
            else:
                os.environ["DOCKET_DELIVERY_HOST"] = previous_mode
        self.assertEqual(adapter.name, "opencode")
