"""Finite read-only contract probes. In-memory fixture DB, fake handler; no listener."""
import json
import sqlite3
import sys
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from docket.api import Handler, MAX_BODY_BYTES, _public_item, _panel_compact, _panel_item
from docket.bounds import check_limits, normalize_created_by
from docket.parse import check_context, check_reason, validate_session_id
from docket.store import Item, Occurrence, Store, SCHEMA

NOW = datetime(2026, 10, 20, tzinfo=timezone.utc)
ROW_BYTES = 12 * 8000 + 8192


def memory_store():
    store = Store.__new__(Store)
    store._db = sqlite3.connect(":memory:")
    store._db.row_factory = sqlite3.Row
    store._db.executescript(SCHEMA)
    store._lock = threading.RLock()
    store._binding_owners = set()
    store._caller_identity = None
    store._tx_depth = 0
    store._commit = store._db.commit  # Fixture-only: no filesystem sealing for :memory:.
    return store


def fixture(i=0, unicode=False):
    reason = check_reason(('😀' if unicode else 'r') * 2000)
    # Exactly 8192 bytes under the accepted context JSON encoding, including
    # supplementary Unicode (12 escaped bytes each), not a guessed UTF-8 budget.
    context = check_context({'x': '😀' * 682}) if unicode else check_context({'x': 'c' * 8184})
    session = 'ses_fixture' + str(i // 20)
    assert validate_session_id(session) is None
    return Item(id=f'dkt_{i:016x}', kind='timer', status='pending', fire_at=NOW,
                created_at=NOW, updated_at=NOW, reason=reason, context=context,
                target={'adapter': 'opencode', 'session_id': session, 'directory': '😀' * 1024,
                        'host': {'unused': '😀' * 10000}}, urgent=False,
                created_by=normalize_created_by('😀' * 200), late=False, late_by_seconds=None,
                delivered_at=None, cancelled_at=None, error=None, receipt=None,
                attempts=0, next_attempt_at=None, firing_at=None, spec=None)


def get(store, path):
    handler = Handler.__new__(Handler)
    handler.path = path
    handler.server = SimpleNamespace(store=store)
    handler._boundary_ok = lambda: True
    answer = []
    handler._send = lambda status, body: answer.append((status, body))
    handler.do_GET()
    return answer[0]


def capacity(unicode=False):
    store = memory_store()
    rows = []
    try:
        for i in range(200):
            item = fixture(i, unicode)
            check_limits(rows, kind='timer', session_id=item.target['session_id'], created_by=None)
            rows.append(item)
            store.insert(item)
        return get(store, '/api/v1/items?status=outstanding&view=panel')[1]
    finally:
        store._db.close()


class PanelReadContract(unittest.TestCase):
    def setUp(self):
        self.store = memory_store()
        self.addCleanup(self.store._db.close)

    def test_capacity_ascii_and_unicode(self):
        for unicode in (False, True):
            body = capacity(unicode)
            self.assertEqual(len(body['items']), 200)
            self.assertFalse(body['window']['truncated'])
            self.assertLess(len(json.dumps(body).encode()), ROW_BYTES * 200 + 4096)
            for row in body['items']:
                self.assertEqual(row['reason'], ('😀' if unicode else 'r') * 2000)
                self.assertEqual(row['delivery']['outcome'], 'pending')
                self.assertIsNone(row['delivery']['facts']['consumed'])
                self.assertNotIn('context', row)
                self.assertNotIn('host', row['target'])

    def test_original_capacity_exceeds_old_cap(self):
        rows = [replace(fixture(i), target={'adapter': 'opencode', 'session_id': 'ses_fixture' + str(i // 20)}, created_by=None)
                for i in range(200)]
        raw = json.dumps({'items': [_public_item(self.store, row) for row in rows]}).encode()
        self.assertEqual(len(raw), 2257011)

    def test_update_recency_not_schedule_time(self):
        for i in range(1000):
            self.store.insert(replace(fixture(i), status='cancelled', fire_at=NOW + timedelta(days=300), updated_at=NOW - timedelta(days=1)))
        failed = replace(fixture(2000), status='undeliverable', fire_at=NOW - timedelta(days=200), updated_at=NOW)
        self.store.insert(failed)
        status, body = get(self.store, '/api/v1/items?view=panel&status=terminal')
        self.assertEqual(status, 200)
        self.assertEqual(body['items'][0]['id'], failed.id)
        self.assertEqual(len(body['items']), 40)
        self.assertTrue(body['window']['truncated'])
        self.assertNotEqual(body['items'][0]['delivery']['outcome'], 'delivered')

    def test_exact_item_windows(self):
        for status, limit in (('pending', 200), ('cancelled', 40)):
            self.store._db.execute('DELETE FROM items')
            query = 'outstanding' if status == 'pending' else 'terminal'
            for i in range(limit):
                self.store.insert(replace(fixture(i), status=status))
            rows, truncated = self.store.panel_items(query)
            self.assertEqual(len(rows), limit)
            self.assertFalse(truncated)
            self.store.insert(replace(fixture(limit), status=status))
            self.assertTrue(self.store.panel_items(query)[1])

    def test_occurrence_window(self):
        parent = fixture()
        self.store.insert(parent)
        for i in range(41):
            occ = Occurrence(id=f'occ_{i:016x}', item_id=parent.id, seq=i, status='cancelled',
                             scheduled_at=NOW, created_at=NOW, updated_at=NOW + timedelta(seconds=i),
                             late=False, late_by_seconds=None, delivered_at=None, error=None,
                             receipt=None, attempts=0, next_attempt_at=None, firing_at=None,
                             coalesced=7, detail={'unused': '😀' * 10000})
            self.store._insert_occurrence(occ)
            if i == 39:
                exact = get(self.store, '/api/v1/occurrences?view=panel&status=all')[1]
                self.assertEqual(len(exact['occurrences']), 40)
                self.assertFalse(exact['window']['truncated'])
        status, body = get(self.store, '/api/v1/occurrences?view=panel&status=all')
        self.assertEqual(status, 200)
        self.assertEqual(len(body['occurrences']), 40)
        self.assertTrue(body['window']['truncated'])
        self.assertEqual(body['occurrences'][0]['seq'], 40)
        self.assertNotIn('detail', body['occurrences'][0])
        self.assertNotEqual(body['occurrences'][0]['delivery']['outcome'], 'delivered')
        self.assertNotIn('window', get(self.store, '/api/v1/occurrences?status=all')[1])

    def test_default_and_filters_unchanged(self):
        row = fixture()
        self.store.insert(row)
        for suffix in ('', '?status=all', '?status=pending', '?status=outstanding', '?outcome=pending'):
            status, body = get(self.store, '/api/v1/items' + suffix)
            self.assertEqual(status, 200)
            self.assertEqual(body, {'items': [_public_item(self.store, row)]})
        self.assertEqual(get(self.store, '/api/v1/items?status=cancelled')[1], {'items': []})
        self.assertEqual(get(self.store, '/api/v1/items?outcome=delivered')[1], {'items': []})
        self.assertIn('recent_occurrences', get(self.store, '/api/v1/items/' + row.id)[1]['item'])

    def test_allowlist_rejects_unknown_duplicate_blank_and_bounds(self):
        for query in ('view=', 'view=other', 'view=panel', 'view=panel&status=all',
                      'view=panel&status=outstanding&limit=201', 'view=panel&status=terminal&sort=fire_at',
                      'view=panel&status=terminal&fields=context', 'view=panel&view=panel&status=terminal',
                      'view=panel&status=terminal&status=outstanding', 'view=panel&status=',
                      'view=panel&status=terminal&extra='):
            self.assertEqual(get(self.store, '/api/v1/items?' + query)[0], 400, query)
        with self.assertRaises(ValueError):
            self.store.panel_items('all')

    def test_compact_detail_shared_delivery_and_explicit_field_truncation(self):
        row = fixture(unicode=True)
        self.store.insert(row)
        _, body = get(self.store, '/api/v1/items/' + row.id + '?view=panel')
        compact = body['item']
        self.assertEqual(compact['delivery'], _public_item(self.store, row)['delivery'])
        self.assertEqual(compact['created_by'], row.created_by)
        self.assertEqual(compact['target']['directory'], '😀' * 1000)
        self.assertTrue(compact['fields_truncated'])
        self.assertLess(len(json.dumps(compact).encode()), ROW_BYTES)
        self.assertNotIn('recent_occurrences', compact)

    def test_maximum_request_metadata_and_worst_escaped_projection(self):
        row = fixture(unicode=True)
        session = 'ses_' + 'x' * 1000  # Accepted session grammar has no length maximum.
        self.assertIsNone(validate_session_id(session))
        request = {'in': '1h', 'reason': row.reason, 'context': row.context,
                   'created_by': row.created_by, 'session_id': session, 'directory': ''}
        remaining = MAX_BODY_BYTES - len(json.dumps(request).encode())
        request['directory'] = '😀' * (remaining // 12) + 'x' * (remaining % 12)
        self.assertEqual(len(json.dumps(request).encode()), MAX_BODY_BYTES)
        row.target = {'session_id': session, 'directory': request['directory']}
        self.store.insert(row)
        compact = get(self.store, '/api/v1/items/' + row.id + '?view=panel')[1]['item']
        self.assertTrue(compact['fields_truncated'])
        self.assertEqual(compact['id'], row.id)
        self.assertEqual(compact['reason'], row.reason)
        self.assertEqual(compact['delivery'], _public_item(self.store, row)['delivery'])
        # Exercise every bounded delivery text slot simultaneously, including
        # supplementary code points at the largest JSON escape expansion.
        public = _public_item(self.store, row)
        for key in ('issue', 'check_issue', 'receipt_issue'):
            public['delivery'][key] = {field: '😀' * 10000 for field in ('code', 'detail', 'phase', 'admission_certainty')}
        bounded = _panel_compact(public)
        self.assertTrue(bounded['fields_truncated'])
        self.assertLess(len(json.dumps(bounded).encode()), ROW_BYTES)
        self.assertEqual(bounded['delivery']['facts'], public['delivery']['facts'])

    def test_compact_recurring_uses_shared_projection_without_global_history_counts(self):
        parent = replace(fixture(), kind='recurring', spec={'every': 60, 'open_occurrence_id': 'occ_0000000000000002'})
        self.store.insert(parent)
        for i, status in enumerate(('delivered', 'undeliverable', 'pending')):
            self.store._insert_occurrence(Occurrence(
                id=f'occ_{i:016x}', item_id=parent.id, seq=i, status=status,
                scheduled_at=NOW, created_at=NOW, updated_at=NOW, late=False,
                late_by_seconds=None, delivered_at=None, error=None, receipt=None,
                attempts=0, next_attempt_at=None, firing_at=None, coalesced=7, detail=None))
        public = _public_item(self.store, parent)
        self.store.occurrence_counts = lambda: self.fail('compact read must not scan unused global counts')
        compact = _panel_item(self.store, parent)
        self.assertEqual(compact['delivery'], public['delivery'])
        self.assertEqual(compact['open_occurrence']['id'], public['open_occurrence']['id'])
        self.assertEqual(compact['delivery_summary'], public['delivery_summary'])
        self.assertEqual(compact['delivery_summary']['delivered'], 1)
        self.assertEqual(compact['coalesced_slots'], 21)


if __name__ == '__main__':
    if sys.argv[1:] == ['--fixtures']:
        # Preserve actual Handler._send's ensure_ascii wire encoding for the relay probe.
        print(json.dumps({'ascii': json.dumps(capacity()), 'unicode': json.dumps(capacity(True))}))
    else:
        unittest.main()
