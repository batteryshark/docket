"""Mocked CLI and client contract tests for delivery recovery and exact-unit operations."""

from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

from docket.cli import main
from docket.client import ClientError, request


class ClientRecoveryTests(unittest.TestCase):
    def test_client_error_attributes_and_payload_preservation(self):
        payload = {"error": {"code": "already_consumed", "detail": "consumed"}, "authorized": False}
        err = ClientError("consumed", status=409, code="already_consumed", payload=payload)
        self.assertEqual(err.detail, "consumed")
        self.assertEqual(err.status, 409)
        self.assertEqual(err.code, "already_consumed")
        self.assertEqual(err.payload, payload)
        self.assertEqual(str(err), "consumed")

        simple = ClientError("connection lost")
        self.assertEqual(simple.detail, "connection lost")
        self.assertIsNone(simple.status)
        self.assertIsNone(simple.code)
        self.assertIsNone(simple.payload)

        non_dict = ClientError("bad", payload=["not", "a", "dict"])
        self.assertIsNone(non_dict.payload)

    @patch("docket.client.resolve_token", return_value="token123")
    @patch("docket.client.resolve_url", return_value="http://127.0.0.1:8768")
    def test_request_preserves_409_503_structured_payload(self, _url, _token):
        refusal_payload = {
            "error": {"code": "already_consumed", "detail": "The wake was already consumed."},
            "operation": "retry",
            "authorized": False,
            "item": {"id": "dkt_test1", "status": "delivered"},
        }
        raw_body = json.dumps(refusal_payload).encode()
        http_err = urllib.error.HTTPError(
            url="http://127.0.0.1:8768/api/v1/items/dkt_test1/retry",
            code=409,
            msg="Conflict",
            hdrs={},
            fp=io.BytesIO(raw_body),
        )

        with patch("urllib.request.urlopen", side_effect=http_err):
            with self.assertRaises(ClientError) as caught:
                request("POST", "/api/v1/items/dkt_test1/retry", {})
            exc = caught.exception
            self.assertEqual(exc.status, 409)
            self.assertEqual(exc.code, "already_consumed")
            self.assertEqual(exc.detail, "The wake was already consumed.")
            self.assertEqual(exc.payload, refusal_payload)

        # Also verify 503 unavailable refusal
        unavailable_payload = {
            "error": {"code": "unavailable", "detail": "Backend unavailable."},
            "operation": "reconcile",
            "authorized": False,
            "item": {"id": "dkt_test1", "status": "pending"},
        }
        raw_503 = json.dumps(unavailable_payload).encode()
        http_503 = urllib.error.HTTPError(
            url="http://127.0.0.1:8768/api/v1/items/dkt_test1/reconcile",
            code=503,
            msg="Service Unavailable",
            hdrs={},
            fp=io.BytesIO(raw_503),
        )
        with patch("urllib.request.urlopen", side_effect=http_503):
            with self.assertRaises(ClientError) as caught:
                request("POST", "/api/v1/items/dkt_test1/reconcile", {})
            exc = caught.exception
            self.assertEqual(exc.status, 503)
            self.assertEqual(exc.code, "unavailable")
            self.assertEqual(exc.detail, "Backend unavailable.")
            self.assertEqual(exc.payload, unavailable_payload)

    @patch("docket.client.resolve_token", return_value="token123")
    @patch("docket.client.resolve_url", return_value="http://127.0.0.1:8768")
    def test_request_malformed_and_credential_safety(self, _url, _token):
        # Malformed HTML error containing sensitive-looking string
        html_raw = b"<html><head><title>502 Gateway Error</title></head><body>Secret token=oc_secret_raw</body></html>"
        http_502 = urllib.error.HTTPError(
            url="http://127.0.0.1:8768/api/v1/items",
            code=502,
            msg="Bad Gateway",
            hdrs={},
            fp=io.BytesIO(html_raw),
        )
        with patch("urllib.request.urlopen", side_effect=http_502):
            with self.assertRaises(ClientError) as caught:
                request("GET", "/api/v1/items")
            exc = caught.exception
            self.assertEqual(exc.status, 502)
            self.assertIsNone(exc.code)
            self.assertIsNone(exc.payload)
            self.assertEqual(exc.detail, "HTTP 502")
            self.assertNotIn("Secret", exc.detail)
            self.assertNotIn("<html>", exc.detail)

        # Non-string nested error detail must not be stringified into diagnostics
        nested_error = {"error": {"code": "bad_req", "detail": {"nested": "dict_value", "secret": "s"}}}
        http_400 = urllib.error.HTTPError(
            url="http://127.0.0.1:8768/api/v1/items",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=io.BytesIO(json.dumps(nested_error).encode()),
        )
        with patch("urllib.request.urlopen", side_effect=http_400):
            with self.assertRaises(ClientError) as caught:
                request("GET", "/api/v1/items")
            exc = caught.exception
            self.assertEqual(exc.status, 400)
            self.assertEqual(exc.detail, "HTTP 400")
            self.assertNotIn("nested", exc.detail)
            self.assertNotIn("dict_value", exc.detail)

    @patch("docket.client.resolve_token", return_value="token123")
    @patch("docket.client.resolve_url", return_value="http://127.0.0.1:8768")
    def test_request_network_errors_stay_fixed_unreachable(self, _url, _token):
        for exc_to_raise in (
            urllib.error.URLError("Connection refused"),
            TimeoutError("timed out"),
            OSError("network down"),
        ):
            with patch("urllib.request.urlopen", side_effect=exc_to_raise):
                with self.assertRaises(ClientError) as caught:
                    request("GET", "/health")
                self.assertEqual(caught.exception.detail, "docket is not reachable")


class CliRecoveryTests(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.get("DOCKET_URL"), os.environ.get("DOCKET_TOKEN")
        os.environ["DOCKET_URL"] = "http://127.0.0.1:8768"
        os.environ["DOCKET_TOKEN"] = "cli-token"

    def tearDown(self):
        for key, val in zip(("DOCKET_URL", "DOCKET_TOKEN"), self._old_env):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(argv)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 2
        return code, out.getvalue(), err.getvalue()

    @patch("docket.cli.request")
    def test_reconcile_and_retry_routes_and_empty_body(self, mock_request):
        # 1. Item reconcile
        mock_request.return_value = (200, {
            "item": {"id": "dkt_abc1", "status": "pending", "delivery": {"outcome": "pending"}},
            "operation": "reconcile",
            "authorized": False,
        })
        code, out, _err = self._run(["reconcile", "dkt_abc1"])
        self.assertEqual(code, 0)
        mock_request.assert_called_with("POST", "/api/v1/items/dkt_abc1/reconcile", {})
        self.assertIn("reconciled dkt_abc1", out)
        self.assertIn("status=pending", out)
        self.assertIn("outcome=pending", out)

        # 2. Occurrence reconcile
        mock_request.reset_mock()
        mock_request.return_value = (200, {
            "occurrence": {"id": "occ_def2", "status": "undeliverable", "delivery": {"outcome": "failed"}},
            "operation": "reconcile",
            "authorized": False,
        })
        code, out, _err = self._run(["reconcile", "occ_def2", "--occurrence"])
        self.assertEqual(code, 0)
        mock_request.assert_called_with("POST", "/api/v1/occurrences/occ_def2/reconcile", {})
        self.assertIn("reconciled occ_def2", out)
        self.assertIn("status=undeliverable", out)
        self.assertIn("outcome=failed", out)

        # 3. Item retry
        mock_request.reset_mock()
        mock_request.return_value = (202, {
            "item": {"id": "dkt_abc1", "status": "pending", "delivery": {"outcome": "pending"}},
            "operation": "retry",
            "authorized": True,
        })
        code, out, _err = self._run(["retry", "dkt_abc1"])
        self.assertEqual(code, 0)
        mock_request.assert_called_with("POST", "/api/v1/items/dkt_abc1/retry", {})
        self.assertIn("authorized retry for dkt_abc1 (not delivered)", out)

        # 4. Occurrence retry
        mock_request.reset_mock()
        mock_request.return_value = (202, {
            "occurrence": {"id": "occ_def2", "status": "pending", "delivery": {"outcome": "pending"}},
            "operation": "retry",
            "authorized": True,
        })
        code, out, _err = self._run(["retry", "occ_def2", "--occurrence"])
        self.assertEqual(code, 0)
        mock_request.assert_called_with("POST", "/api/v1/occurrences/occ_def2/retry", {})
        self.assertIn("authorized retry for occ_def2 (not delivered)", out)

    @patch("docket.cli.request")
    def test_unit_id_url_encoding(self, mock_request):
        mock_request.return_value = (200, {
            "item": {"id": "dkt/foo#1?bar=baz", "status": "pending", "delivery": {"outcome": "pending"}},
        })
        code, _out, _err = self._run(["reconcile", "dkt/foo#1?bar=baz"])
        self.assertEqual(code, 0)
        mock_request.assert_called_with("POST", "/api/v1/items/dkt%2Ffoo%231%3Fbar%3Dbaz/reconcile", {})

        mock_request.reset_mock()
        mock_request.return_value = (202, {
            "occurrence": {"id": "occ 123/special", "status": "pending"},
            "authorized": True,
        })
        code, _out, _err = self._run(["retry", "occ 123/special", "--occurrence"])
        self.assertEqual(code, 0)
        mock_request.assert_called_with("POST", "/api/v1/occurrences/occ%20123%2Fspecial/retry", {})

    def test_flag_rejections_and_no_overrides(self):
        # Must never accept replacement ID, target, reason, text, time, urgency, caller, or force
        rejected_commands = [
            ["retry", "dkt_1", "--force"],
            ["retry", "dkt_1", "--reason", "new reason"],
            ["retry", "dkt_1", "--session", "ses_override"],
            ["retry", "dkt_1", "--urgent"],
            ["retry", "dkt_1", "--by", "ses_other"],
            ["reconcile", "dkt_1", "--force"],
            ["reconcile", "dkt_1", "--reason", "audit"],
            ["reconcile", "dkt_1", "--session", "ses_new"],
            ["reconcile", "dkt_1", "--directory", "/other"],
        ]
        for argv in rejected_commands:
            with self.subTest(argv=argv):
                code, _out, err = self._run(argv)
                self.assertEqual(code, 2)
                self.assertIn("invalid arguments", err)

    @patch("docket.cli.request")
    def test_retry_202_outputs_authorization_only_not_delivered(self, mock_request):
        mock_request.return_value = (202, {
            "item": {"id": "dkt_item_due", "status": "pending", "delivery": {"outcome": "pending"}},
            "operation": "retry",
            "authorized": True,
        })
        code, out, err = self._run(["retry", "dkt_item_due"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("authorized retry for dkt_item_due (not delivered)", out)
        self.assertNotIn("delivered dkt_item_due", out)

        # With --json
        code, out_json, _err = self._run(["retry", "dkt_item_due", "--json"])
        self.assertEqual(code, 0)
        parsed = json.loads(out_json)
        self.assertTrue(parsed["authorized"])
        self.assertEqual(parsed["item"]["id"], "dkt_item_due")

    @patch("docket.cli.request")
    def test_409_503_refusal_json_payload_and_nonzero(self, mock_request):
        # 409 already_consumed refusal
        refusal_409 = {
            "error": {"code": "already_consumed", "detail": "The wake was already consumed."},
            "operation": "retry",
            "authorized": False,
            "item": {
                "id": "dkt_consumed1",
                "status": "delivered",
                "delivery": {"outcome": "delivered"},
            },
        }
        mock_request.side_effect = ClientError(
            "The wake was already consumed.",
            status=409,
            code="already_consumed",
            payload=refusal_409,
        )

        # In --json mode: exit code 1, full structured refusal on stdout as valid JSON
        code, out, err = self._run(["retry", "dkt_consumed1", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        parsed = json.loads(out)
        self.assertEqual(parsed["error"]["code"], "already_consumed")
        self.assertFalse(parsed["authorized"])
        self.assertEqual(parsed["item"]["delivery"]["outcome"], "delivered")
        self.assertNotIn("docket:", out)  # Not stringified into plain error message

        # In text mode: exit code 1, stderr formatted
        code, out, err = self._run(["retry", "dkt_consumed1"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("docket: The wake was already consumed.", err)

        # 503 unavailable on reconcile
        mock_request.reset_mock()
        refusal_503 = {
            "error": {"code": "unavailable", "detail": "The delivery provider could not be reached."},
            "operation": "reconcile",
            "authorized": False,
            "item": {
                "id": "dkt_unavail1",
                "status": "pending",
                "delivery": {"outcome": "uncertain", "issue": {"code": "unavailable"}},
            },
        }
        mock_request.side_effect = ClientError(
            "The delivery provider could not be reached.",
            status=503,
            code="unavailable",
            payload=refusal_503,
        )

        code, out, _err = self._run(["reconcile", "dkt_unavail1", "--json"])
        self.assertEqual(code, 1)
        parsed_503 = json.loads(out)
        self.assertEqual(parsed_503["error"]["code"], "unavailable")
        self.assertEqual(parsed_503["item"]["delivery"]["outcome"], "uncertain")

    @patch("docket.cli.request")
    def test_malformed_error_fallback(self, mock_request):
        # ClientError with no structured payload (e.g. HTML 500 from proxy)
        mock_request.side_effect = ClientError("HTTP 500", status=500, code=None, payload=None)

        # In --json mode: exit code 1, safe fallback JSON on stdout
        code, out, err = self._run(["reconcile", "dkt_err1", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        parsed = json.loads(out)
        self.assertEqual(parsed, {"error": {"detail": "HTTP 500"}})

        # In text mode: exit code 1, safe generic detail on stderr
        code, out, err = self._run(["reconcile", "dkt_err1"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("docket: HTTP 500", err)

    @patch("docket.cli.request")
    def test_list_differing_lifecycle_and_delivery_outcome(self, mock_request):
        # Differing lifecycle and delivery outcomes:
        # Item 1: status="pending", outcome="admitted"
        # Item 2: status="completed", outcome="not_applicable" (completed parent, NOT consumed!)
        # Item 3: status="pending", outcome="uncertain" with safe issue
        # Item 4: legacy item without delivery dict -> outcome="unknown"
        items = [
            {
                "id": "dkt_admitted1",
                "kind": "timer",
                "status": "pending",
                "fire_at": "2026-10-20T12:00:00Z",
                "target": {"session_id": "ses_target1"},
                "reason": "awaiting admission",
                "delivery": {"outcome": "admitted"},
            },
            {
                "id": "dkt_parent1",
                "kind": "recurring",
                "status": "completed",
                "fire_at": "2026-10-20T12:00:00Z",
                "target": {"session_id": "ses_target2"},
                "reason": "recurring parent finished",
                "spec": {"every": 60},
                "delivery": {"outcome": "not_applicable"},
            },
            {
                "id": "dkt_uncertain1",
                "kind": "alarm",
                "status": "pending",
                "fire_at": "2026-10-20T12:00:00Z",
                "target": {"session_id": "ses_target3"},
                "reason": "unclear delivery",
                "delivery": {
                    "outcome": "uncertain",
                    "issue": {"code": "backend_unverifiable", "detail": "lost namespace"},
                },
            },
            {
                "id": "dkt_legacy1",
                "kind": "timer",
                "status": "pending",
                "fire_at": "2026-10-20T12:00:00Z",
                "target": {"session_id": "ses_target4"},
                "reason": "legacy row",
            },
        ]
        mock_request.return_value = (200, {"items": items})

        code, out, _err = self._run(["list"])
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 4)

        # Check line 1: lifecycle status is pending, delivery outcome is admitted
        self.assertIn("pending", lines[0])
        self.assertIn("outcome=admitted", lines[0])

        # Check line 2: lifecycle status is completed, outcome is not_applicable (never inferred consumed)
        self.assertIn("completed", lines[1])
        self.assertIn("outcome=not_applicable", lines[1])

        # Check line 3: lifecycle status is pending, outcome is uncertain, safe issue shown
        self.assertIn("pending", lines[2])
        self.assertIn("outcome=uncertain", lines[2])
        self.assertIn("issue=backend_unverifiable:lost namespace", lines[2])

        # Check line 4: legacy item without projection maps to unknown
        self.assertIn("pending", lines[3])
        self.assertIn("outcome=unknown", lines[3])

        # Test optional --outcome query parameter
        mock_request.reset_mock()
        mock_request.return_value = (200, {"items": [items[0]]})
        code, _out, _err = self._run(["list", "--outcome", "admitted"])
        self.assertEqual(code, 0)
        mock_request.assert_called_with("GET", "/api/v1/items?status=pending&outcome=admitted")

        # Test combined --status and --outcome
        mock_request.reset_mock()
        mock_request.return_value = (200, {"items": []})
        code, _out, _err = self._run(["list", "--status", "all", "--outcome", "delivered"])
        self.assertEqual(code, 0)
        mock_request.assert_called_with("GET", "/api/v1/items?status=all&outcome=delivered")

    @patch("docket.cli.request")
    def test_occurrences_differing_lifecycle_and_delivery_outcome(self, mock_request):
        occs = [
            {
                "id": "occ_failed1",
                "item_id": "dkt_series1",
                "seq": 1,
                "status": "undeliverable",
                "scheduled_at": "2026-10-20T12:00:00Z",
                "delivery": {
                    "outcome": "failed",
                    "issue": {"code": "unavailable", "detail": "connection lost"},
                },
            },
            {
                "id": "occ_ok2",
                "item_id": "dkt_series1",
                "seq": 2,
                "status": "delivered",
                "scheduled_at": "2026-10-20T12:05:00Z",
                "delivery": {"outcome": "delivered"},
            },
            {
                "id": "occ_legacy3",
                "item_id": "dkt_series1",
                "seq": 3,
                "status": "pending",
                "scheduled_at": "2026-10-20T12:10:00Z",
                # no delivery field
            },
        ]
        mock_request.return_value = (200, {"occurrences": occs})

        code, out, _err = self._run(["occurrences", "dkt_series1"])
        self.assertEqual(code, 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 3)

        self.assertIn("undeliverable", lines[0])
        self.assertIn("outcome=failed", lines[0])
        self.assertIn("issue=unavailable:connection lost", lines[0])

        self.assertIn("delivered", lines[1])
        self.assertIn("outcome=delivered", lines[1])

        self.assertIn("pending", lines[2])
        self.assertIn("outcome=unknown", lines[2])

    @patch("docket.cli.request")
    def test_cancel_distinguishes_schedule_stopped_and_retraction_outcomes(self, mock_request):
        # 1. not_sent
        mock_request.return_value = (200, {
            "item": {"id": "dkt_c1", "status": "cancelled"},
            "schedule_stopped": True,
            "retraction": {"outcome": "not_sent"},
        })
        code, out, _err = self._run(["cancel", "dkt_c1"])
        self.assertEqual(code, 0)
        self.assertIn("cancelled dkt_c1 (schedule_stopped, retraction=not_sent)", out)
        self.assertNotIn("Removed", out)

        # 2. retracted
        mock_request.return_value = (200, {
            "item": {"id": "dkt_c2", "status": "cancelled"},
            "schedule_stopped": True,
            "retraction": {"outcome": "retracted"},
        })
        code, out, _err = self._run(["cancel", "dkt_c2"])
        self.assertEqual(code, 0)
        self.assertIn("cancelled dkt_c2 (schedule_stopped, retraction=retracted)", out)
        self.assertNotIn("Removed", out)

        # 3. too_late
        mock_request.return_value = (200, {
            "item": {"id": "dkt_c3", "status": "cancelled"},
            "schedule_stopped": True,
            "retraction": {"outcome": "too_late"},
        })
        code, out, _err = self._run(["cancel", "dkt_c3"])
        self.assertEqual(code, 0)
        self.assertIn("cancelled dkt_c3 (schedule_stopped, retraction=too_late)", out)
        self.assertNotIn("Removed", out)

        # 4. uncertain
        mock_request.return_value = (200, {
            "item": {"id": "dkt_c4", "status": "cancelled"},
            "schedule_stopped": True,
            "retraction": {"outcome": "uncertain"},
        })
        code, out, _err = self._run(["cancel", "dkt_c4"])
        self.assertEqual(code, 0)
        self.assertIn("cancelled dkt_c4 (schedule_stopped, retraction=uncertain)", out)
        self.assertNotIn("Removed", out)

        # 5. cancel with --json preserves complete payload
        code, out_json, _err = self._run(["cancel", "dkt_c4", "--json"])
        self.assertEqual(code, 0)
        parsed = json.loads(out_json)
        self.assertTrue(parsed["schedule_stopped"])
        self.assertEqual(parsed["retraction"]["outcome"], "uncertain")

        # 6. cancel on non-outstanding item returns 409 and preserved JSON
        cancel_409 = {
            "error": {"code": "already_consumed", "detail": "The wake was already consumed."},
            "item": {"id": "dkt_c5", "status": "delivered"},
            "schedule_stopped": False,
            "retraction": {"outcome": "uncertain"},
        }
        mock_request.side_effect = ClientError(
            "The wake was already consumed.",
            status=409,
            code="already_consumed",
            payload=cancel_409,
        )
        code, out_409, _err = self._run(["cancel", "dkt_c5", "--json"])
        self.assertEqual(code, 1)
        parsed_409 = json.loads(out_409)
        self.assertEqual(parsed_409["error"]["code"], "already_consumed")
        self.assertFalse(parsed_409["schedule_stopped"])
        self.assertEqual(parsed_409["retraction"]["outcome"], "uncertain")

    def test_credential_protections_preserved(self):
        # OpenChamber connection links and tokens must not be passed as arguments
        bad_args = [
            ["pair", "openchamber:link_data_here"],
            ["pair", "oc_client_secret_data"],
            ["timer", "--session", "ses_1", "--reason", "openchamber:link_data", "--in", "5m"],
            ["reconcile", "oc_client_link_accident"],
            ["retry", "openchamber:accident"],
        ]
        for argv in bad_args:
            with self.subTest(argv=argv):
                code, _out, err = self._run(argv)
                self.assertEqual(code, 2)
                self.assertIn("Connection links are never accepted as arguments", err)


if __name__ == "__main__":
    unittest.main()
