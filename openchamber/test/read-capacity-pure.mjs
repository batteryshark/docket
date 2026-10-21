// Real Core-shaped bytes through the actual relay/UI, with fake I/O only.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
import {stripTypeScriptTypes} from 'node:module';
import vm from 'node:vm';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const root = fileURLToPath(new URL('../../', import.meta.url));
const fixtures = JSON.parse(execFileSync('python3', ['-m', 'tests.test_panel_read_contract', '--fixtures'], {
  cwd: root, env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}, maxBuffer: 20_000_000,
}));
let assertions = 0;
const check = fn => { fn(); assertions++; };
const window = (limit, order, truncated = false) => ({view: 'panel', limit, order, truncated});
let history = {items: [], window: window(40, 'updated_at_desc,id_desc')};
let occurrences = {occurrences: [], window: window(40, 'updated_at_desc,id_desc')};
let current;
let currentWire;
let historyMode = 'ok';
let occurrenceMode = 'ok';
let cancelled = 0;
const requests = [];
const relay = vm.createContext({Buffer, URL, AbortSignal, Set, Map, Date, Object, path,
  process: {env: {OPENCHAMBER_SERVICE_PORT: '0', OPENCHAMBER_SERVICE_TOKEN: 'fake', DOCKET_HOME: '/fake', DOCKET_URL: 'http://127.0.0.1:9876', DOCKET_TOKEN: 'fake-core'}, platform: 'darwin'},
  os: {homedir: () => '/fake'}, fs: {readFileSync() { throw Error('No real files'); }},
  timingSafeEqual: (a, b) => a.equals(b), http: {createServer() { return {}; }},
  fetch: async (url, options) => {
    requests.push({url, options});
    const pending = url.includes('status=outstanding');
    const isHistory = url.includes('status=terminal');
    const isOccurrences = url.includes('/occurrences?');
    const body = url.includes('/meta') ? {} : pending ? current : isHistory ? history : occurrences;
    const bytes = (isHistory && historyMode === 'oversize') || (isOccurrences && occurrenceMode === 'oversize')
      ? Buffer.alloc(40 * (12 * 8000 + 8192) + 4097, 32) : Buffer.from(pending ? currentWire : JSON.stringify(body));
    let offset = 0;
    return {status: (isHistory && historyMode === 'unavailable') || (isOccurrences && occurrenceMode === 'unavailable') ? 503 : 200,
      body: {getReader: () => ({read: async () => {
        if (offset >= bytes.length) return {done: true};
        const value = bytes.subarray(offset, offset += 16384);
        return {done: false, value};
      }, cancel: async () => { cancelled++; }})}};
  },
});
vm.runInContext(readFileSync(path.join(root, 'openchamber/service/main.mjs'), 'utf8').replace(/^import .*;\n/gm, '').split('server.listen(')[0], relay);
const snapshot = async () => {
  const result = await vm.runInContext('snapshot()', relay);
  assert.equal(result.status, 200);
  return JSON.parse(result.body);
};
let result;
for (const name of ['ascii', 'unicode']) {
  currentWire = fixtures[name];
  current = JSON.parse(currentWire);
  check(() => assert.ok(Buffer.byteLength(currentWire) > 2_000_000));
  result = await snapshot();
  check(() => assert.equal(result.outstanding.length, 200));
  check(() => assert.equal(result.outstanding_limited, false));
  check(() => assert.equal(result.outstanding_window_unavailable, false));
  check(() => assert.equal(result.history_truncated, false));
  for (let i = 0; i < 200; i++) {
    assert.equal(result.outstanding[i].reason, current.items[i].reason);
    assert.equal(result.outstanding[i].id, current.items[i].id);
    assert.deepEqual(result.outstanding[i].delivery.facts, current.items[i].delivery.facts);
    assert.equal(result.outstanding[i].delivery.outcome, 'pending');
  }
  check(() => assert.equal(Array.from(result.outstanding[0].reason).length, 2000));
}
for (const mode of ['unavailable', 'oversize']) {
  historyMode = mode;
  result = await snapshot();
  check(() => assert.equal(result.outstanding.length, 200));
  check(() => assert.equal(result.items_history_unavailable, true));
  check(() => assert.equal(result.history_items.length, 0));
}
check(() => assert.equal(cancelled, 1));
historyMode = 'ok';
for (const mode of ['unavailable', 'oversize']) {
  occurrenceMode = mode;
  result = await snapshot();
  check(() => assert.equal(result.outstanding.length, 200));
  check(() => assert.equal(result.history_unavailable, true));
}
occurrenceMode = 'ok';
occurrences = {occurrences: []}; // Missing window is not complete/empty success.
result = await snapshot();
check(() => assert.equal(result.history_unavailable, true));
occurrences.window = window(40, 'updated_at_desc,id_desc');
history = {items: [{...current.items[0], status: 'undeliverable', fire_at: '2020-01-01', updated_at: '2026-10-20'},
  {...current.items[1], status: 'cancelled', fire_at: '2027-01-01', updated_at: '2026-10-19'}], window: window(40, 'updated_at_desc,id_desc', true)};
result = await snapshot();
check(() => assert.equal(result.history_items[0].id, current.items[0].id));
check(() => assert.equal(result.history_truncated, true));
check(() => assert.equal(result.history_items[0].delivery.outcome, 'pending')); // lifecycle never supplies delivery
history.window.truncated = false;
history.window.unused = {raw: 'PRIVATE_WINDOW_DIAGNOSTIC'};
result = await snapshot();
check(() => assert.equal(result.history_truncated, false));
check(() => assert.ok(!JSON.stringify(result).includes('PRIVATE_WINDOW_DIAGNOSTIC')));
check(() => assert.ok(requests.every(({url}) => url.includes('/meta') || url.includes('view=panel'))));
check(() => assert.ok(requests.every(({options}) => options.method === 'GET' && !options.headers.authorization)));

class Element {
  children = []; dataset = {}; textContent = ''; className = '';
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  set innerHTML(_) { throw Error('Inert DOM required'); }
}
const nodes = new Map();
const panel = vm.createContext({Set, Map, Date, Object, JSON, console,
  connectHost: () => ({onReady() {}, setBadge: async () => {}}), HostRequestError: class extends Error {},
  document: {getElementById: id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); }, createElement: () => new Element(), documentElement: new Element(), addEventListener() {}},
  window: {setTimeout: () => 0, setInterval: () => 0}, clearTimeout() {}, clearInterval() {},
  IntersectionObserver: class {observe() {}}, mountButton() {}, mountBadge() {}, mountBanner() {}, mountEmpty() {}, mountSpinner() {}, mountTabs() {}, applyHostReady() {},
});
const ts = readFileSync(path.join(root, 'openchamber/panel/main.ts'), 'utf8').replace(/^import .*;\n/gm, '');
vm.runInContext(stripTypeScriptTypes(ts, {mode: 'strip'}), panel);
panel.fixture = result;
vm.runInContext('snapshot = fixture; paint()', panel);
// Count by reason nodes, independent of provisional card styling.
const reasons = [];
function visit(node) { if (node.className === 'reason') reasons.push(node.textContent); for (const child of node.children || []) visit(child); }
for (const node of nodes.values()) visit(node);
check(() => assert.equal(reasons.length, 200));
check(() => assert.ok(reasons.every(reason => reason === '😀'.repeat(2000))));
result.items_history_unavailable = true;
result.fields_truncated = true;
vm.runInContext('paint()', panel);
const text = [];
function collect(node) { text.push(node.textContent); for (const child of node.children || []) collect(child); }
for (const node of nodes.values()) collect(node);
check(() => assert.ok(text.some(value => value.includes('optional history is unavailable'))));
check(() => assert.ok(text.some(value => value.includes('metadata was shortened'))));
console.log(`read-capacity-pure: ${assertions} checks plus 200 per-row identity/reason/fact assertions per encoding passed`);
