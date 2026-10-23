"""Current merged EventWorker -> EventHost wire regression; no model/network.

Subprocess uses only this checkout's merged modules, with no adjacent worktree.
Only the native HTTP boundary is faked, not either side of the lifecycle contract.
"""
from pathlib import Path
import subprocess
import sys
import unittest


class CrossLaneTests(unittest.TestCase):
    def test_real_worker_passes_epoch_lifecycle_to_real_host(self):
        script = r'''
import time
from docket.event_host import EventHost
from tests.test_wake_profiles import EventHostTest
from docket.subscription_worker import EventStore, EventWorker
f = EventHostTest(); f.setUp()
store = EventStore(f.root / 'events.db')
worker = EventWorker(store, f.host)
try:
    token = 'fixture-token-' + 'x' * 40
    store.install_publisher('fixture', token)
    store.install_subscription('sub', 'fixture', 'completed', {'action':'owner','baseline':f.b,'profile':f.p})
    receipt = store.accept(token, {'id':'event-one','type':'completed','payload':{'result':'bad'}})
    worker.tick(time.time() + 0.1)
    state = store.state(receipt['id'])
    assert state['phase'] == 'admitted', (state, store.inspect_events())
    sent = [body for method,path,body in f.calls if path.endswith('/prompt')]
    assert len(sent) == 1 and sent[0]['id'] == receipt['admission_id'], sent
    binding = __import__('json').loads((f.root / 'ses_test.json').read_text())
    assert binding['delivery_id'] == receipt['id'] != receipt['admission_id']
finally:
    worker.close(); store.close(); f.tearDown()
'''
        result = subprocess.run([sys.executable, '-B', '-c', script],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
