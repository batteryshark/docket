// Finite VM/fixture tests only. No browser, network, listener, service or timer.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import {stripTypeScriptTypes} from 'node:module';
import {EventEmitter} from 'node:events';

const root = new URL('../', import.meta.url);
const itemId = 'dkt_' + '1'.repeat(16);
const occId = 'occ_' + '2'.repeat(16);
const msgId = 'msg_' + '3'.repeat(32);
const hostile = '<img src=x onerror=alert(1)>';
const projection = (outcome = 'uncertain', retry = false, unit_id = itemId) => ({
  unit_id, request_id: msgId, outcome, phase: 'check', execution: 'unknown',
  facts: {admitted: null, claimed: null, injected: null, consumed: null, cancelled: null},
  evidence: 'request', stale: true, last_checked_at: null,
  issue: {code: 'acceptance_unknown', detail: 'An earlier send may have been accepted.'},
  check_issue: {code: 'unavailable', detail: 'Provider unavailable'}, receipt_issue: null,
  namespace_verified: false, same_daemon_recovery: false,
  recovery: {reconcile: true, retry, refusal_code: retry ? null : 'acceptance_unknown'},
});
const item = (outcome = 'uncertain') => ({id: itemId, kind: 'timer', status: 'ambiguous', reason: hostile,
  created_by: hostile, target: {session_id: hostile, directory: hostile}, delivery: projection(outcome), frozen: 'SECRET'});
let assertions = 0;
function check(fn) { fn(); assertions++; }

const source = readFileSync(new URL('service/main.mjs', root), 'utf8');
let handler;
const requests = [];
let response = () => ({status: 200, body: {}});
const relay = vm.createContext({Buffer, URL, AbortSignal, Set, Map, Date, Object,
  process: {env: {OPENCHAMBER_SERVICE_PORT: '0', OPENCHAMBER_SERVICE_TOKEN: 'HOST_SECRET', DOCKET_HOME: '/fixture', DOCKET_URL: 'http://127.0.0.1:9876', DOCKET_TOKEN: 'DOCKET_SECRET'}, platform: 'darwin'},
  path, os: {homedir: () => '/home/fixture'}, fs: {readFileSync() { throw Error('Unexpected file read'); }},
  timingSafeEqual: (a, b) => a.equals(b),
  http: {createServer(fn) { handler = fn; return {}; }},
  fetch: async (url, options) => {
    requests.push({url, ...options});
    const answer = response(url, options);
    if (answer.throw) throw Error('SECRET companion stderr');
    let used = false;
    return {status: answer.status, body: {getReader: () => ({read: async () => used ? {done: true} : (used = true, {done: false, value: Buffer.from(JSON.stringify(answer.body))}), cancel: async () => {}})}};
  },
});
vm.runInContext(source.replace(/^import .*;\n/gm, '').split('server.listen(')[0], relay);
const run = code => vm.runInContext(code, relay);
relay.fixture = item();
check(() => assert.equal(run('viewItem(fixture).delivery.outcome'), 'uncertain'));
check(() => assert.equal(run('viewItem({...fixture, status:"completed", delivery:undefined}).delivery'), null));
check(() => assert.equal(run('deliveryView({...fixture.delivery, request_id:"legacy_PRIVATE"}).request_id'), null));
check(() => assert.equal(run('deliveryView({...fixture.delivery, request_id:[fixture.delivery.request_id]}).request_id'), null));
check(() => assert.equal(run('deliveryView({...fixture.delivery, request_id:fixture.delivery.request_id + "\\n"}).request_id'), null));
check(() => assert.equal(run('deliveryView({...fixture.delivery, unit_id:[fixture.id]})'), null));
check(() => assert.equal(run('deliveryView({...fixture.delivery, recovery:{retry:true}})'), null));
check(() => assert.equal(run('deliveryView({...fixture.delivery, outcome:"lost"})'), null));
check(() => assert.equal(run('deliveryView({...fixture.delivery, facts:{}})'), null));
check(() => assert.ok(!run('JSON.stringify(viewItem(fixture))').includes('SECRET')));
relay.series = {...item(), kind: 'recurring', status: 'completed', open_occurrence: {id: occId}, delivery: projection('uncertain', false, occId)};
check(() => assert.equal(run('viewItem(series).unit_scope'), 'occurrences'));
check(() => assert.equal(run('viewItem({...series, open_occurrence:null}).unit_scope'), null));
run('process.env.DOCKET_HOME = "~/custom"');
check(() => assert.equal(run('docketHome()'), '/home/fixture/custom'));
run('delete process.env.DOCKET_HOME; process.platform = "win32"; process.env.LOCALAPPDATA = "C:\\u005cUsers\\u005cfixture\\u005cLocal"');
check(() => assert.equal(run('docketHome()'), path.join('C:\\Users\\fixture\\Local', 'Docket')));
run('process.env.LOCALAPPDATA = "C:relative"');
check(() => assert.equal(run('docketHome()'), null));
run('process.platform = "darwin"; process.env.DOCKET_HOME = "/fixture"');

async function route(routePath, body = '{}', status = 200, answer = {}) {
  response = () => ({status, body: answer});
  const req = new EventEmitter();
  Object.assign(req, {method: 'POST', url: routePath, headers: {authorization: 'Bearer HOST_SECRET'}});
  let result;
  const res = {writeHead(value) { result = {status: value}; }, end(raw) { result.body = JSON.parse(raw); }};
  const pending = handler(req, res);
  req.emit('data', Buffer.from(body)); req.emit('end');
  await pending;
  return result;
}
for (const scope of ['items', 'occurrences']) for (const action of ['reconcile', 'retry']) {
  const id = scope === 'items' ? itemId : occId;
  const result = await route(`/${scope}/${id}/${action}`, '{}', 409, {error: {code: 'in_flight', detail: 'Still running'}, item: item()});
  check(() => assert.equal(result.status, 409));
  check(() => assert.equal(result.body.authorized, false));
  check(() => assert.equal(result.body.error.code, 'in_flight'));
  check(() => assert.equal(requests.at(-1).url, `http://127.0.0.1:9876/api/v1/${scope}/${id}/${action}`));
  check(() => assert.equal(requests.at(-1).body, '{}'));
  check(() => assert.equal(requests.at(-1).headers.authorization, 'Bearer DOCKET_SECRET'));
}
for (const bad of ['{"force":true}', '{"text":"new"}', '{"id":"new"}', '{"target":"new","time":0,"urgency":true,"caller":"new"}', '[]', 'null']) {
  const before = requests.length;
  const rejected = await route(`/items/${itemId}/retry`, bad);
  check(() => assert.equal(rejected.status, 400));
  check(() => assert.equal(requests.length, before));
}
for (const bad of [`/items/${itemId}/retry?force=true`, '/items/../retry', `/occurrences/${itemId}/retry`, `/items/${itemId}%2fother/retry`]) {
  const before = requests.length;
  assert.ok((await route(bad)).status >= 400);
  check(() => assert.equal(requests.length, before));
}
let result = await route(`/items/${itemId}/retry`, '{}', 202, {authorized: true, item: {...item('failed'), delivery: projection('failed', true)}});
check(() => assert.equal(result.body.authorized, true));
check(() => assert.equal(result.body.item.delivery.outcome, 'failed'));
result = await route(`/items/${itemId}/reconcile`, '{}', 503, {error: {code: 'unavailable', detail: 'Provider unavailable'}, item: item()});
check(() => assert.equal(result.status, 503));
check(() => assert.equal(result.body.item.delivery.stale, true));
for (const retraction of ['not_sent', 'retracted', 'too_late', 'uncertain']) {
  result = await route(`/items/${itemId}/remove`, '{}', 200, {schedule_stopped: true, retraction: {outcome: retraction}, item: {...item(), status: 'cancelled'}});
  check(() => assert.equal(result.body.schedule_stopped, true));
  check(() => assert.equal(result.body.retraction, retraction));
  check(() => assert.equal(result.body.item.delivery.outcome, 'uncertain'));
  check(() => assert.equal(result.body.removed, undefined));
}
result = await route(`/items/${itemId}/retry`, '{}', 503, {error: 'SECRET raw stderr', frozen: 'SECRET'});
check(() => assert.ok(!JSON.stringify(result).includes('SECRET')));
result = await route(`/items/${itemId}/reconcile`, '{}', 200, {item: {...item(), reason: 'DOCKET_SECRET HOST_SECRET'}});
check(() => assert.ok(!JSON.stringify(result).includes('DOCKET_SECRET')));
check(() => assert.ok(!JSON.stringify(result).includes('HOST_SECRET')));

const occurrences = ['delivered', 'failed', 'uncertain'].map((outcome, i) => ({id: 'occ_' + String(i + 4).repeat(16), item_id: itemId, status: 'undeliverable', delivery: projection(outcome, false, 'occ_' + String(i + 4).repeat(16))}));
response = url => ({status: 200, body: url.includes('/occurrences?') ? {occurrences} : url.includes('/meta') ? {version: 'fixture'} : {items: [{...item(), kind: 'recurring', status: 'completed', delivery: projection('not_applicable'), delivery_summary: {delivered: 1, failed: 1, uncertain: 1}, coalesced_slots: 8}]}});
result = JSON.parse((await run('snapshot()')).body);
check(() => assert.deepEqual(result.history_occurrences.map(row => row.delivery.outcome), ['delivered', 'failed', 'uncertain']));
check(() => assert.equal(result.history_items[0].delivery.outcome, 'not_applicable'));
check(() => assert.equal(result.history_items[0].coalesced_slots, 8));
check(() => assert.equal(result.history_limited, true));
response = url => ({status: url.includes('/occurrences?') ? 503 : 200, body: {items: []}});
result = JSON.parse((await run('snapshot()')).body);
check(() => assert.equal(result.history_unavailable, true));
response = url => ({status: 200, body: url.includes('/occurrences?') ? {occurrences: [null]} : {items: []}});
result = JSON.parse((await run('snapshot()')).body);
check(() => assert.equal(result.history_unavailable, true));
response = url => ({status: 200, body: url.includes('/occurrences?') ? {occurrences: Array(45).fill(occurrences[0])} : {items: []}});
result = JSON.parse((await run('snapshot()')).body);
check(() => assert.equal(result.history_occurrences.length, 40));
check(() => assert.equal(result.history_truncated, true));

class Element {
  children = []; dataset = {}; textContent = ''; className = '';
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  set innerHTML(_) { throw Error('Unsafe innerHTML'); }
}
const nodes = new Map();
const buttons = [];
const notices = [];
const sent = [];
let resolveSend;
let rejectSend;
const host = {onReady() {}, setBadge: async () => {}, serviceRequest: request => {
  sent.push(request);
  if (request.method === 'GET') return Promise.resolve({status: 503, body: '{}'});
  return new Promise((resolve, reject) => { resolveSend = resolve; rejectSend = reject; });
}};
const panel = vm.createContext({Set, Map, Date, Object, JSON, console,
  connectHost: () => host, HostRequestError: class extends Error {},
  document: {getElementById: id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); }, createElement: () => new Element(), documentElement: new Element(), addEventListener() {}},
  window: {setTimeout: () => 0, setInterval: () => 0}, clearTimeout() {}, clearInterval() {},
  IntersectionObserver: class {observe() {}},
  mountButton: (element, options) => { buttons.push(options); element.append(new Element()); },
  mountBadge() {}, mountBanner: (_, notice) => notices.push(notice), mountEmpty() {}, mountSpinner() {}, mountTabs() {}, applyHostReady() {},
});
const ts = readFileSync(new URL('panel/main.ts', root), 'utf8').replace(/^import .*;\n/gm, '');
vm.runInContext(stripTypeScriptTypes(ts, {mode: 'strip'}), panel);
panel.row = {...item(), unit_scope: 'items'};
const ui = code => vm.runInContext(code, panel);
check(() => assert.equal(ui('stateOf({...row, status:"completed", delivery:null}).key'), 'unknown'));
ui('itemCard(row)');
check(() => assert.equal(buttons.find(b => b.label === 'Safe retry').disabled, true));
check(() => assert.equal(buttons.find(b => b.label === 'Check status').disabled, false));
panel.row.delivery = projection('failed', true);
const pending = ui('recover(row, "retry")');
await ui('recover(row, "retry")');
check(() => assert.equal(sent.length, 1));
ui('evidence(document.createElement("div"), row)');
check(() => assert.equal(buttons.at(-1).disabled, true));
resolveSend({status: 202, body: JSON.stringify({authorized: true, item: {delivery: projection('failed', true)}})});
await pending;
check(() => assert.ok(notices.some(n => n.title === 'Retry authorized — not delivered')));
check(() => assert.equal(sent.filter(s => s.method === 'POST').length, 1));
buttons.length = 0;
ui('evidence(document.createElement("div"), row, true)');
check(() => assert.equal(buttons.find(b => b.label === 'Safe retry').disabled, true));
const card = ui('itemCard(row)');
function texts(element) { return [element.textContent, ...element.children.flatMap(texts)].filter(Boolean); }
check(() => assert.ok(texts(card).includes(hostile)));
for (const status of [409, 503]) {
  const work = ui('recover(row, "retry")');
  resolveSend({status, body: JSON.stringify({error: {code: 'in_flight', detail: hostile}, authorized: true, item: {delivery: projection()}})});
  await work;
  check(() => assert.ok(notices.some(n => n.title === `Operation refused or unavailable (${status})`)));
  panel.row.delivery = projection('failed', true);
}
const beforeTimeout = sent.filter(s => s.method === 'POST').length;
const timedOut = ui('recover(row, "retry")');
rejectSend(Error('SECRET raw timeout'));
await timedOut;
check(() => assert.equal(sent.filter(s => s.method === 'POST').length, beforeTimeout + 1));
check(() => assert.ok(notices.some(n => n.title === 'Operation result unknown')));
check(() => assert.ok(!JSON.stringify(notices).includes('SECRET')));
for (const retraction of ['uncertain', 'too_late', 'not_sent']) {
  panel.itemId = itemId;
  const removing = ui('remove(itemId)');
  await ui('remove(itemId)');
  resolveSend({status: 200, body: JSON.stringify({schedule_stopped: true, retraction, item: {...item(), status: 'cancelled'}})});
  await removing;
  check(() => assert.ok(notices.some(n => n.title === 'Schedule stopped' && (retraction !== 'uncertain' || n.body.includes('late wake')))));
  check(() => assert.ok(!notices.some(n => n.title === 'Removed')));
}
console.log(`recovery-pure: ${assertions} checks passed (finite mocks; no external effects)`);
