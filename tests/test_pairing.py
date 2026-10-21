"""Enrollment and desktop-host tests use fake loopback hosts ONLY."""

import base64
import hashlib
import io
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from docket.delivery import DeliveryError, OpenCodeAdapter, _assert_loopback
from docket.host import AutoAdapter, OpenChamberAdapter, configured_adapter, desktop_origin
from docket.pairing import (
    PairingError, atomic_private, identity, intake, json_request, metadata_warnings, parse_link,
    read_private, redeem,
)

# ECMAScript WhiteSpace + LineTerminator, including BOM (not Python isspace).
JS_TRIM_CHARS = "\t\n\v\f\r \u00a0\u1680" + "".join(map(chr, range(0x2000, 0x200b))) + "\u2028\u2029\u202f\u205f\u3000\ufeff"


class Host(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.calls = []
        self.client = {"id": "docket-own-id", "label": "Docket", "deviceName": "Docket",
                       "clientKind": "desktop", "revokedAt": None, "expiresAt": None}
        self.token = "oc_client_fake_OWN_not_real"
        self.used = False
        self.status = 200
        self.redeem_fields = {}
        self.redirect = False
        self.clients = None
        self.inbox = []
        self.consumed = False
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server_port}"

    def stop(self):
        self.shutdown()
        self.server_close()
        self.thread.join()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def do_DELETE(self):
        self.handle_request()

    def handle_request(self):
        host = self.server
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        host.calls.append((self.command, self.path, self.headers.get("Authorization"), body))
        status = 200
        if self.path == "/health":
            result = {"openchamberVersion": "fake"}
        elif self.path == "/api/client-auth/pairing/redeem":
            status = 400 if host.used else host.status
            host.used = True
            result = {"ok": True, "client": host.client, "clientToken": host.token,
                      "server": {"url": host.origin, "fingerprint": "fake-fingerprint"}}
            result.update(host.redeem_fields)
        elif self.path == "/api/client-auth/clients":
            result = {"clients": host.clients if host.clients is not None else [host.client]}
        elif self.path.endswith("/prompt"):
            host.inbox.append(body)
            result = {"data": {"id": body["id"], "delivery": body["delivery"]}}
        elif "/message/" in self.path:
            status = 200 if host.consumed else 404
            result = {"data": {"id": self.path.rsplit("/", 1)[1]}}
        elif self.path.endswith("/inbox"):
            result = {"data": host.inbox}
        else:
            status = host.status
            result = {"data": {"id": "ses_fake", "time": {}, "location": {}}}
        if host.redirect:
            status = 307
        raw = json.dumps(result).encode()
        self.send_response(status)
        if host.redirect:
            self.send_header("Location", host.origin + "/redirect-leak")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "own"
        self.host = Host()
        self.settings = Path(self.temp.name) / "settings.json"
        self.settings.write_text(json.dumps({"desktopLocalPort": self.host.server_port,
                                            "desktopLocalClientToken": "NEVER_USE"}))

    def tearDown(self):
        self.host.stop()
        self.temp.cleanup()

    def link(self, **changes):
        data = {"pairingId": "pair_fake", "secret": "fake-one-use", "fingerprint": "fake-fingerprint",
                "expiresAt": datetime.fromtimestamp(time.time() + 590, timezone.utc).isoformat(),
                "candidates": [{"type": "lan", "url": self.host.origin, "priority": 10}]}
        data.update(changes)
        return "openchamber://connect?v=2&p=" + base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    def enrolled(self):
        redeem(self.link(), self.root)
        return OpenChamberAdapter(root=self.root, settings=self.settings)

    def test_redeem_single_post_own_fields_private_binding(self):
        report = redeem(self.link(), self.root)
        posts = [c for c in self.host.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 1)
        body = posts[0][3]
        self.assertEqual(body["clientKind"], "desktop")
        self.assertEqual(body["clientLabel"], "Docket")
        self.assertEqual(body["deviceName"], "Docket")
        self.assertEqual(body["dedupeKey"], identity(self.root))
        self.assertNotIn(self.host.token, json.dumps(report))
        binding = read_private(self.root / "client.json")
        self.assertEqual(binding["clientToken"], self.host.token)
        self.assertNotIn("secret", binding)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root / "client.json").stat().st_mode), 0o600)
        self.assertEqual((self.root / "client.json").stat().st_uid, os.getuid())

    def test_expired_and_future_links_never_post(self):
        for delta in (-1, 601):
            link = self.link(expiresAt=datetime.fromtimestamp(time.time() + delta, timezone.utc).isoformat())
            with self.assertRaises(PairingError):
                redeem(link, self.root)
        self.assertEqual(self.host.calls, [])

    def test_principal_labels_and_renamed_own_device_do_not_strand_delivery(self):
        for index, label in enumerate(("Docket", "docket", "DoCkEt", "Principal's reminder 🦈", "\u00a0My device\ufeff")):
            with self.subTest(label=label):
                root = Path(self.temp.name) / f"label-{index}"
                self.host.used = False
                self.host.client.update(label=label)
                link = self.link(label=label)
                self.assertEqual(parse_link(link)[0]["label"], label)
                report = redeem(link, root)
                self.assertTrue(report["paired"])
                self.assertEqual(self.host.client["label"], label)
                self.assertEqual(read_private(root / "client.json")["clientToken"], self.host.token)
                if index % 2:
                    # Older version-1 files retain display fields; these must not
                    # become an exact-label requirement when read by new code.
                    binding = read_private(root / "client.json")
                    binding.update(label=label, clientKind="desktop", fingerprint="old-display-value")
                    atomic_private(root / "client.json", binding)
                # A subsequent UI rename and unknown/missing display metadata cannot
                # change the identity of this same own client id.
                self.host.client.update(label="Renamed device", deviceName="Different name",
                                        clientKind="future-kind", unknown={"display": True})
                adapter = OpenChamberAdapter(root=root, settings=self.settings)
                receipt = adapter.deliver({"session_id": "ses_fake"}, "fake wake",
                                          urgent=False, admission_id=f"msg_fake_{index}")
                self.assertEqual(receipt.delivery, "queue")
                self.assertNotIn(self.host.token, json.dumps([report, adapter.host_status()]))

    def test_minimal_response_and_unknown_display_metadata_are_accepted(self):
        cases = [
            {"client": {"id": "docket-own-id"}, "server": None, "ok": None},
            {"client": dict(self.host.client, label={"unknown": True}, deviceName=None,
                            clientKind="unknown"),
             "server": {"url": "https://untrusted.invalid/path", "fingerprint": "different"},
             "unknown": ["display metadata"]},
        ]
        for index, fields in enumerate(cases):
            with self.subTest(index=index):
                root = Path(self.temp.name) / f"metadata-{index}"
                self.host.used = False
                self.host.redeem_fields = fields
                def checked_after_save(result, data, origin):
                    saved = read_private(root / "client.json")
                    self.assertEqual(saved["clientToken"], self.host.token)
                    self.assertEqual(saved["enrolledOrigin"], self.host.origin)
                    self.assertEqual(stat.S_IMODE((root / "client.json").stat().st_mode), 0o600)
                    return metadata_warnings(result, data, origin)
                with patch("docket.pairing.metadata_warnings", side_effect=checked_after_save):
                    report = redeem(self.link(), root)
                self.assertTrue(report["paired"])
                self.assertTrue(report["warnings"])
                self.assertNotIn("untrusted.invalid", json.dumps(report))
                self.assertNotIn(self.host.token, json.dumps(report))
                self.host.clients = [fields["client"]]
                adapter = OpenChamberAdapter(root=root, settings=self.settings)
                self.assertTrue(adapter.validate({"session_id": "ses_fake"}).ok)

    def test_post_save_check_failure_cannot_lose_or_replace_token(self):
        link = self.link()
        with patch("docket.pairing.metadata_warnings", side_effect=PairingError("synthetic optional failure")):
            with self.assertRaises(PairingError):
                redeem(link, self.root)
        before = (self.root / "client.json").read_bytes()
        self.assertEqual(read_private(self.root / "client.json")["clientToken"], self.host.token)
        with self.assertRaises(PairingError):
            redeem(link, self.root)
        self.assertEqual((self.root / "client.json").read_bytes(), before)
        self.assertEqual(sum(c[0] == "POST" for c in self.host.calls), 1)

    def test_essential_status_id_and_token_rejected_without_retry(self):
        cases = [(status, {}) for status in (201, 400, 401, 503)]
        cases += [(200, {"client": client}) for client in
                  (None, {}, {"id": None}, {"id": ""}, {"id": 123}, {"id": "bad\nid"})]
        cases += [(200, {"clientToken": token}) for token in
                  (None, "", "other_token", "oc_client_", "oc_client_bad\n", 123)]
        for index, (status, fields) in enumerate(cases):
            with self.subTest(index=index):
                root = Path(self.temp.name) / f"invalid-{index}"
                self.host.used = False
                self.host.status, self.host.redeem_fields = status, fields
                before = sum(c[0] == "POST" for c in self.host.calls)
                link = self.link()
                for _ in range(2):
                    with self.assertRaises(PairingError):
                        redeem(link, root)
                self.assertFalse((root / "client.json").exists())
                self.assertEqual(sum(c[0] == "POST" for c in self.host.calls), before + 1)

    def test_minted_token_saved_even_if_record_has_revocation_or_expiry(self):
        for index, field in enumerate(("revokedAt", "expiresAt")):
            with self.subTest(field=field):
                root = Path(self.temp.name) / f"inactive-{index}"
                self.host.used = False
                self.host.redeem_fields = {"client": dict(self.host.client, **{field: "now"})}
                report = redeem(self.link(), root)
                self.assertTrue(report["warnings"])
                self.assertEqual(read_private(root / "client.json")["clientToken"], self.host.token)
                self.host.clients = [self.host.redeem_fields["client"]]
                adapter = OpenChamberAdapter(root=root, settings=self.settings)
                self.assertFalse(adapter.validate({"session_id": "ses_fake"}).ok)
                self.assertEqual(adapter.host_status()["state"], "refused")

    def test_strict_link_validation(self):
        urls = ["http://example.com:80", "http://localhost:80", "http://127.0.0.1.evil:80",
                "http://127.1:80", "http://2130706433:80", "http://user@127.0.0.1:80",
                "https://127.0.0.1:80", "http://127.0.0.1:80/x", "http://127.0.0.1:80?x=y",
                "http://127.0.0.1:80#fragment", "http://[::ffff:127.0.0.1]:80"]
        links = [self.link(candidates=[{"type": "lan", "url": u}]) for u in urls]
        links += [self.link().replace("v=2", "v=1"), self.link() + "&v=2", self.link() + "#x",
                  self.link(candidates=[]), self.link(candidates=[{"type": "relay", "url": self.host.origin}]),
                  self.link(secret="\nsecret"), self.link(fingerprint=None), self.link(expiresAt="bad")]
        for link in links:
            with self.subTest(link=link), self.assertRaises(PairingError):
                parse_link(link)
        self.assertEqual(self.host.calls, [])

    def test_reuse_locally_refused_and_host_used_link_fails(self):
        link = self.link()
        redeem(link, self.root)
        with self.assertRaises(PairingError):
            redeem(link, self.root)
        with self.assertRaises(PairingError):
            redeem(link, Path(self.temp.name) / "second")
        self.assertEqual(len([c for c in self.host.calls if c[0] == "POST"]), 2)

    def test_failed_redemption_is_not_retried(self):
        self.host.status = 503
        link = self.link()
        for _ in range(2):
            with self.assertRaises(PairingError):
                redeem(link, self.root)
        self.assertEqual(len([c for c in self.host.calls if c[0] == "POST"]), 1)
        self.assertFalse((self.root / "client.json").exists())

    def test_nonissued_credentials_rejected_before_network_or_state(self):
        from docket.cli import main
        index = 0
        for field, original in (("pairingId", "pair_fake"), ("secret", "fake-one-use")):
            for char in JS_TRIM_CHARS + "\x7fé🦈":
                for value in (char + original, original + char):
                    with self.subTest(field=field, codepoint=ord(char), index=index):
                        root = Path(self.temp.name) / f"invalid-credential-{index}"
                        index += 1
                        link = self.link(**{field: value})
                        with patch("docket.pairing.json_request") as network:
                            with self.assertRaises(PairingError) as caught:
                                redeem(link, root)
                            network.assert_not_called()
                        self.assertEqual(str(caught.exception), "invalid Connection link")
                        self.assertFalse(root.exists())
        # The public stdin path must not print the link or decoded credentials.
        link = self.link(pairingId="\u00a0pair_fake", secret="fake-one-use\ufeff")
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"DOCKET_HOME": str(self.root)}), patch("sys.stdin", io.StringIO(link)), patch("sys.stdout", out), patch("sys.stderr", err):
            self.assertNotEqual(main(["pair", "--stdin"]), 0)
        for sensitive in (link, "pair_fake", "fake-one-use", self.host.token):
            self.assertNotIn(sensitive, out.getvalue() + err.getvalue())
        self.assertFalse(self.root.exists())
        self.assertEqual(self.host.calls, [])

    def test_host_trim_equivalent_credentials_do_not_resubmit(self):
        link = self.link()
        real = json_request
        def lost(origin, method, path, *args, **kwargs):
            result = real(origin, method, path, *args, **kwargs)
            if method == "POST":
                raise PairingError("synthetic lost response")
            return result
        with patch("docket.pairing.json_request", side_effect=lost):
            with self.assertRaises(PairingError):
                redeem(link, self.root)
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        calls = list(self.host.calls)
        for field, original in (("pairingId", "pair_fake"), ("secret", "fake-one-use")):
            for char in JS_TRIM_CHARS:
                for value in (char + original, original + char):
                    with self.subTest(field=field, codepoint=ord(char)):
                        with patch("docket.pairing.json_request") as network:
                            with self.assertRaises(PairingError) as caught:
                                redeem(self.link(**{field: value}), self.root)
                            network.assert_not_called()
                        self.assertEqual(str(caught.exception), "invalid Connection link")
        self.assertEqual(self.host.calls, calls)
        self.assertEqual(sum(c[0] == "POST" for c in self.host.calls), 1)
        self.assertEqual({p.name: p.read_bytes() for p in self.root.iterdir()}, before)
        for sensitive in (link, "pair_fake", "fake-one-use", self.host.token):
            self.assertNotIn(sensitive, b"".join(before.values()).decode())
        self.assertFalse((self.root / "client.json").exists())

    def test_unknown_outcome_equivalent_enrollments_never_post_again(self):
        link = self.link(v=2)
        data, _ = parse_link(link)
        encoded = base64.urlsafe_b64encode(json.dumps(dict(reversed(list(data.items()))),
                                                    ensure_ascii=True, indent=2).encode()).decode().rstrip("=")
        variants = [link, link.replace("?v=2&p=", "?p=") + "&v=2",
                    "openchamber://connect?p=" + encoded + "&v=%32",
                    link.replace("v=2", "%76=%32")]
        # JSON escape spelling also decodes to precisely the same enrollment.
        escaped = json.dumps(data).replace("pair_fake", "\\u0070air_fake")
        variants.append("openchamber://connect?v=2&p=" + base64.urlsafe_b64encode(escaped.encode()).decode().rstrip("="))
        # Accepted base64 spellings can differ only in unused trailing bits.
        raw = json.dumps(data).encode()
        while len(raw) % 3 != 1:
            raw += b" "
        encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        alternate_bits = encoded[:-1] + alphabet[alphabet.index(encoded[-1]) | 1]
        variants.append("openchamber://connect?v=2&p=" + alternate_bits)
        real = json_request
        def lost(origin, method, path, *args, **kwargs):
            if method == "POST":
                ledger = read_private(self.root / "attempt.json")
                self.assertEqual(ledger["version"], 2)  # Durable before submission.
                self.assertEqual(len(ledger["enrollmentDigests"]), 1)
                real(origin, method, path, *args, **kwargs)
                raise PairingError("synthetic lost response")
            return real(origin, method, path, *args, **kwargs)
        with patch("docket.pairing.json_request", side_effect=lost):
            with self.assertRaises(PairingError):
                redeem(link, self.root)
        for alternate in variants:
            with self.subTest(alternate=variants.index(alternate)):
                self.assertEqual(parse_link(alternate), parse_link(link))
                with self.assertRaises(PairingError):
                    redeem(alternate, self.root)
        self.assertEqual(sum(c[0] == "POST" for c in self.host.calls), 1)
        raw = (self.root / "attempt.json").read_text()
        for secret in (link, data["secret"], data["pairingId"]):
            self.assertNotIn(secret, raw)
        self.assertFalse((self.root / "client.json").exists())

    def test_legacy_or_invalid_attempt_ledgers_require_review(self):
        link = self.link()
        raw_digest = hashlib.sha256(link.encode()).hexdigest()
        for ledger in ({"linkDigests": [raw_digest]}, {"linkDigests": []}, {},
                       {"version": 2}, {"version": 2, "enrollmentDigests": []},
                       {"version": 2, "enrollmentDigests": [None]},
                       {"version": 2, "enrollmentDigests": ["a" * 64], "linkDigests": [raw_digest]}):
            atomic_private(self.root / "attempt.json", ledger)
            before = (self.root / "attempt.json").read_bytes()
            for candidate in (link, link.replace("?v=2&p=", "?p=") + "&v=2", self.link(secret="different")):
                with self.assertRaisesRegex(PairingError, "operator review"):
                    redeem(candidate, self.root)
            self.assertEqual((self.root / "attempt.json").read_bytes(), before)
        self.assertEqual(self.host.calls, [])

    def test_attempt_write_failure_prevents_post(self):
        real = atomic_private
        def failed(path, value):
            if path.name == "attempt.json":
                raise OSError("synthetic write failure")
            real(path, value)
        with patch("docket.pairing.atomic_private", side_effect=failed), self.assertRaises(OSError):
            redeem(self.link(), self.root)
        self.assertFalse(any(c[0] == "POST" for c in self.host.calls))

    def test_redirect_not_followed(self):
        self.host.redirect = True
        with self.assertRaises(PairingError):
            redeem(self.link(), self.root)
        self.assertEqual(len(self.host.calls), 1)  # health redirect stopped before POST
        status, _ = json_request(self.host.origin, "POST", "/redirect-test", {}, self.host.token)
        self.assertEqual(status, 307)
        self.assertFalse(any(c[1] == "/redirect-leak" for c in self.host.calls))

    def test_file_symlinks_modes_and_ownership_rejected(self):
        self.enrolled()
        path = self.root / "client.json"
        path.chmod(0o644)
        with self.assertRaises(PairingError):
            read_private(path)
        path.chmod(0o600)
        original = os.fstat
        def foreign(fd):
            st = original(fd)
            parts = list(st)
            parts[4] = os.getuid() + 1
            return os.stat_result(parts)
        with patch("docket.pairing.os.fstat", side_effect=foreign), self.assertRaises(PairingError):
            read_private(path)
        path.unlink()
        path.symlink_to(self.settings)
        with self.assertRaises(OSError):
            read_private(path)
        with self.assertRaises(OSError):
            atomic_private(path, {})

    def test_atomic_failure_keeps_prior_binding_and_cleans_temp(self):
        self.enrolled()
        path = self.root / "client.json"
        before = path.read_bytes()
        with patch("docket.pairing.os.replace", side_effect=OSError("fake")), self.assertRaises(OSError):
            atomic_private(path, {"replacement": True})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".client-*")), [])
        self.root.chmod(0o755)
        with self.assertRaises(PairingError):
            read_private(path)

    def test_guard_revocation_expiry_mismatch_multi_no_fallback(self):
        adapter = self.enrolled()
        self.assertTrue(adapter.validate({"session_id": "ses_fake"}).ok)
        for clients in ([], [dict(self.host.client, revokedAt="now")],
                        [dict(self.host.client, expiresAt="tomorrow")],
                        [dict(self.host.client, id="other")], [self.host.client, self.host.client]):
            self.host.clients = clients
            before = len([c for c in self.host.calls if c[1] == "/api/session/ses_fake"])
            self.assertFalse(adapter.validate({"session_id": "ses_fake"}).ok)
            self.assertEqual(adapter.host_status()["state"], "refused")
            self.assertEqual(len([c for c in self.host.calls if c[1] == "/api/session/ses_fake"]), before)
        self.assertTrue(all(c[2] == "Bearer " + self.host.token for c in self.host.calls if c[1].startswith("/api/") and c[0] == "GET"))

    def test_port_rollover_and_auth_failure(self):
        adapter = self.enrolled()
        second = Host()
        try:
            self.settings.write_text(json.dumps({"desktopLocalPort": second.server_port}))
            self.assertTrue(adapter.validate({"session_id": "ses_fake"}).ok)
            self.assertEqual(adapter.host_status()["origin"], second.origin)
            second.status = 401
            with self.assertRaises(DeliveryError):
                adapter._request("GET", "/api/session/active")
            self.assertEqual(adapter.host_status()["state"], "refused")
        finally:
            second.stop()

    def test_unpaired_explicit_and_generic_default_binding_cannot_downgrade(self):
        with patch.dict(os.environ, {"DOCKET_HOME": self.temp.name, "DOCKET_DELIVERY_HOST": "auto"}):
            self.assertIsInstance(configured_adapter(), AutoAdapter)
        adapter = OpenChamberAdapter(paired=False, root=self.root, settings=self.settings)
        self.assertTrue(adapter.validate({"session_id": "ses_fake"}).ok)
        self.assertIsNone(self.host.calls[-1][2])
        self.assertEqual(adapter.host_status()["mode"], "unpaired-loopback")
        with patch.dict(os.environ, {"DOCKET_HOME": self.temp.name, "DOCKET_DELIVERY_HOST": "opencode"}):
            root = Path(self.temp.name) / "openchamber"
            identity(root)
            self.assertIsInstance(configured_adapter(), OpenChamberAdapter)

    def test_settings_numeric_only_and_no_credential_use(self):
        for value in (True, "53170", None, -1, 65536, 1.2):
            self.settings.write_text(json.dumps({"desktopLocalPort": value}))
            with self.assertRaises(PairingError):
                desktop_origin(self.settings)

    def test_stdin_bounded_and_cli_does_not_echo(self):
        self.assertEqual(intake(io.StringIO("hello\n")), "hello")
        with self.assertRaises(PairingError):
            intake(io.StringIO("x" * 65537))
        from docket.cli import main
        out, err = io.StringIO(), io.StringIO()
        link = self.link()
        with patch.dict(os.environ, {"DOCKET_HOME": self.temp.name}), patch("sys.stdin", io.StringIO(link)), patch("sys.stdout", out), patch("sys.stderr", err):
            self.assertEqual(main(["pair", "--stdin"]), 0)
        self.assertNotIn(link, out.getvalue() + err.getvalue())
        self.assertNotIn(self.host.token, out.getvalue() + err.getvalue())
        err = io.StringIO()
        with patch("sys.stderr", err):
            self.assertEqual(main(["pair", "--stdin", link]), 2)
        self.assertNotIn(link, err.getvalue())

    def test_cli_errors_never_echo_accidental_argv_in_any_position(self):
        from docket.cli import main
        markers = ["openchamber://connect?v=2&p=SYNTHETIC_ONLY", "oc_client_SYNTHETIC_ONLY",
                   "SYNTHETIC_ONLY"]
        for marker in markers:
            arrangements = [["--unknown", "pair", "--stdin", marker],
                            [marker, "pair", "--stdin"], ["--unknown=" + marker],
                            ["pair", marker, "--stdin"], ["serve", "--port", marker],
                            ["service", marker], ["--unknown", marker, "--help"]]
            for argv in arrangements:
                out, err = io.StringIO(), io.StringIO()
                with patch("sys.stdout", out), patch("sys.stderr", err):
                    try:
                        code = main(argv)
                    except SystemExit as exc:
                        code = exc.code
                self.assertIn(code, (0, 2))  # argparse help may exit before validation.
                self.assertNotIn(marker, out.getvalue() + err.getvalue())
                self.assertNotIn("SYNTHETIC_ONLY", out.getvalue() + err.getvalue())
        for argv in (["--help"], ["pair", "--help"], ["pair", "-h"]):
            out = io.StringIO()
            with patch("sys.stdout", out), self.assertRaises(SystemExit) as raised:
                main(argv)
            self.assertEqual(raised.exception.code, 0)
            self.assertIn("--stdin" if "pair" in argv else "Local schedule", out.getvalue())

    def test_generic_urls_reject_secrets_before_transport_or_status(self):
        urls = [self.host.origin + "/?credential=SYNTHETIC_ONLY",
                self.host.origin.replace("://", "://SYNTHETIC_ONLY@"),
                self.host.origin + "/SYNTHETIC_ONLY", self.host.origin + "#SYNTHETIC_ONLY",
                self.host.origin + "?", self.host.origin + "#", self.host.origin + "\n",
                "ftp://127.0.0.1:80", "http://127.0.0.1:bad"]
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(DeliveryError):
                    _assert_loopback(url)
                adapter = OpenCodeAdapter(discovery=lambda: (url, "Basic SYNTHETIC_ONLY"))
                with patch("urllib.request.urlopen") as transport:
                    with self.assertRaises(DeliveryError) as raised:
                        adapter._request("GET", "/health")
                    transport.assert_not_called()
                self.assertIsNone(adapter.host_status()["origin"])
                self.assertNotIn("SYNTHETIC_ONLY", json.dumps(adapter.host_status()) + str(raised.exception))
        for url in (self.host.origin, self.host.origin + "/", "http://localhost", "https://[::1]:443/"):
            _assert_loopback(url)
        adapter = OpenCodeAdapter(discovery=lambda: (self.host.origin + "/", "Basic SYNTHETIC_ONLY"))
        adapter._request("GET", "/health")
        self.assertEqual(adapter.host_status()["origin"], self.host.origin)

    def test_generic_api_and_store_persist_only_origin(self):
        from docket.api import DocketServer
        from docket.clock import SystemClock
        from docket.scheduler import Scheduler
        from docket.store import Store
        import urllib.request

        adapter = OpenCodeAdapter(discovery=lambda: (self.host.origin + "/", "Basic SYNTHETIC_ONLY"))
        store = Store(Path(self.temp.name) / "generic.db")
        scheduler = Scheduler(store, adapter, SystemClock())
        server = DocketServer(store, scheduler, "fake-inbound", port=0)
        server.serve_in_thread()
        try:
            data = json.dumps({"session_id": "ses_fake", "reason": "fake wake", "in": 0}).encode()
            req = urllib.request.Request(server.url + "/api/v1/timers", data=data,
                                         headers={"Authorization": "Bearer fake-inbound"})
            with urllib.request.urlopen(req) as response:
                item = json.load(response)["item"]
            self.assertEqual(item["target"]["host"]["origin"], self.host.origin)
            scheduler.tick()
            item = store.get(item["id"]).public()
            self.assertEqual(item["receipt"]["host"]["origin"], self.host.origin)
            for path in ("/health", "/api/v1/meta"):
                req = urllib.request.Request(server.url + path, headers={"Authorization": "Bearer fake-inbound"})
                with urllib.request.urlopen(req) as response:
                    status = json.load(response)
                self.assertEqual(status["host"]["origin"], self.host.origin)
                self.assertNotIn("SYNTHETIC_ONLY", json.dumps([status, item]))
        finally:
            server.stop()
            store.close()

    def test_dynamic_enrollment_latches_and_missing_binding_refuses(self):
        adapter = AutoAdapter(root=self.root)
        identity(self.root)
        # No legacy discovery is consulted once enrollment exists.
        with patch("docket.host.desktop_origin", return_value=self.host.origin):
            self.assertFalse(adapter.validate({"session_id": "ses_fake"}).ok)
            self.assertEqual(adapter.host_status()["mode"], "paired")
            (self.root / "identity.json").unlink()
            self.assertFalse(adapter.validate({"session_id": "ses_fake"}).ok)
            self.assertEqual(adapter.host_status()["state"], "refused")

    def test_duplicate_json_rejected(self):
        encoded = base64.urlsafe_b64encode(b'{"secret":"one","secret":"two"}').decode().rstrip("=")
        with self.assertRaises(PairingError):
            parse_link("openchamber://connect?v=2&p=" + encoded)

    def test_paired_queue_steer_and_exact_reconciliation_routes(self):
        adapter = self.enrolled()
        target = {"session_id": "ses_fake"}
        receipt = adapter.deliver(target, "fake wake", urgent=False, admission_id="msg_fake_queue")
        self.assertEqual(receipt.delivery, "queue")
        self.assertEqual(receipt.raw["host"]["origin"], self.host.origin)
        self.assertEqual(adapter.reconcile(target, "msg_fake_queue"), "admitted")
        self.host.consumed = True
        self.assertEqual(adapter.reconcile(target, "msg_fake_queue"), "consumed")
        self.assertEqual(adapter.deliver(target, "fake urgent", urgent=True, admission_id="msg_fake_steer").delivery, "steer")
        self.assertFalse(any(c[1] == "/api/session/active" for c in self.host.calls))
        self.assertEqual([c[3]["delivery"] for c in self.host.calls if c[1].endswith("/prompt")], ["queue", "steer"])

    def test_api_and_store_expose_host_without_token(self):
        from docket.api import DocketServer
        from docket.clock import SystemClock
        from docket.scheduler import Scheduler
        from docket.store import Store
        import urllib.request

        adapter = self.enrolled()
        store = Store(Path(self.temp.name) / "docket.db")
        scheduler = Scheduler(store, adapter, SystemClock())
        server = DocketServer(store, scheduler, "fake-inbound", port=0)
        server.serve_in_thread()
        try:
            data = json.dumps({"session_id": "ses_fake", "reason": "fake wake", "in": 0}).encode()
            req = urllib.request.Request(server.url + "/api/v1/timers", data=data,
                                         headers={"Authorization": "Bearer fake-inbound"})
            with urllib.request.urlopen(req) as response:
                item = json.load(response)["item"]
            self.assertEqual(item["target"]["host"]["origin"], self.host.origin)
            scheduler.tick()
            item = store.get(item["id"]).public()
            self.assertEqual(item["receipt"]["host"]["mode"], "paired")
            with urllib.request.urlopen(server.url + "/health") as response:
                status = json.load(response)
            self.assertEqual(status["host"]["origin"], self.host.origin)
            self.assertNotIn(self.host.token, json.dumps([item, status]))
        finally:
            server.stop()
            store.close()


if __name__ == "__main__":
    unittest.main()
