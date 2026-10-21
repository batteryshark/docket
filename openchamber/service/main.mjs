// Loopback relay from the OpenChamber panel to Docket.
//
// The panel runs in a sandboxed iframe and cannot reach 127.0.0.1. OpenChamber
// starts this process and forwards serviceRequest calls here. This process
// reads the Docket token from disk and attaches it on the way to Docket.
// The token is never written to a response, a log line, or the panel.
//
// The panel does not choose the backend. The URL comes from DOCKET_URL, only
// when that value is loopback, or otherwise from server.json under DOCKET_HOME
// or the platform Docket state root. OpenCode's port is not used here.
// Paths are a fixed list. Reason and context are never copied into a URL,
// a header, or a command, and context is not returned to the iframe.

import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {timingSafeEqual} from 'node:crypto';

const portRaw = process.env.OPENCHAMBER_SERVICE_PORT;
const serviceToken = process.env.OPENCHAMBER_SERVICE_TOKEN || '';
if (portRaw == null || portRaw === '' || !serviceToken) {
  throw new Error('OpenChamber must launch the Docket relay.');
}
const port = Number(portRaw);
if (!Number.isInteger(port) || port < 0 || port > 65535) {
  throw new Error('OPENCHAMBER_SERVICE_PORT is not a port.');
}

const ITEM_ID = /^dkt_[0-9a-f]{16}$/;
const OCC_ID = /^occ_[0-9a-f]{16}$/;
const REQUEST_ID = /^msg_[0-9a-f]{32}$/;
const matches = (pattern, value) => typeof value === 'string' && value.trim() === value && pattern.test(value);
const TIMEOUT_MS = 8000;
const HISTORY_LIMIT = 40;
// Core panel row: <8,000 code points (12 JSON bytes each at worst), plus
// 8,192 bytes for fixed keys/numbers. 4,096 covers the envelope/window.
const PANEL_ROW_BYTES = 12 * 8000 + 8192;
const panelBytes = rows => rows * PANEL_ROW_BYTES + 4096;
const CANCELLABLE = new Set(['pending', 'firing', 'admitted', 'ambiguous']);

function docketHome() {
  if (process.env.DOCKET_HOME) {
    const home = process.env.DOCKET_HOME;
    return path.resolve(home === '~' ? os.homedir() : home.startsWith('~/') || home.startsWith('~\\') ? path.join(os.homedir(), home.slice(2)) : home);
  }
  if (process.platform === 'win32') {
    const local = process.env.LOCALAPPDATA || '';
    const normalized = local.replaceAll('/', '\\');
    const unc = normalized.match(/^\\\\([^\\]+)\\([^\\]+)(?:\\|$)/);
    const qualified = /^[a-z]:\\/i.test(normalized)
      || (unc && ![unc[1], unc[2]].some(part => part === '.' || part === '..' || part === '?'));
    if (!qualified || local.includes('\0')) return null;
    return path.join(local, 'Docket');
  }
  return path.join(os.homedir(), '.docket');
}

function readJson(file) {
  try {
    const value = JSON.parse(fs.readFileSync(file, 'utf8'));
    return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
  } catch {
    return null;
  }
}

function loopbackUrl(value) {
  if (typeof value !== 'string' || !value || value.length > 200) return null;
  let url;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
  if (url.username || url.password || url.search || url.hash) return null;
  if (url.pathname !== '/' && url.pathname !== '') return null;
  const host = url.hostname.replace(/^\[|\]$/g, '');
  if (host !== '127.0.0.1' && host !== 'localhost' && host !== '::1') return null;
  if (!url.port) return null;
  return url.origin;
}

function discover() {
  const home = docketHome();
  if (!home) return {error: 'Docket state location is unavailable'};
  if (process.env.DOCKET_URL) {
    const url = loopbackUrl(process.env.DOCKET_URL);
    if (!url) return {error: 'DOCKET_URL is not a loopback Docket address'};
    return {url, home};
  }
  const info = readJson(path.join(home, 'server.json'));
  const url = loopbackUrl(info && info.url);
  if (!url) return {error: 'Docket server URL is not a loopback address in server.json'};
  return {url, home};
}

function readDocketToken(home) {
  if (process.env.DOCKET_TOKEN) return String(process.env.DOCKET_TOKEN);
  const file = process.env.DOCKET_TOKEN_FILE || path.join(home, 'token');
  try {
    return fs.readFileSync(file, 'utf8').trim();
  } catch {
    return '';
  }
}

function authorized(header) {
  const actual = Buffer.from(header || '');
  const expected = Buffer.from('Bearer ' + serviceToken);
  if (actual.length !== expected.length) return false;
  return timingSafeEqual(actual, expected);
}

function scrub(text, secret) {
  let out = typeof text === 'string' ? text : '';
  if (secret && out.includes(secret)) out = out.split(secret).join('[redacted]');
  if (serviceToken && out.includes(serviceToken)) out = out.split(serviceToken).join('[redacted]');
  return out;
}

function clip(text, max) {
  const value = typeof text === 'string' ? text : '';
  return value.length > max ? Array.from(value).slice(0, max).join('') : value;
}

function jsonSend(status, payload, secret) {
  const encoded = JSON.stringify(payload);
  return {status, body: scrub(encoded, secret)};
}

async function docketFetch(method, docketPath, {auth = false, secret = '', maxBytes = 2_000_000} = {}) {
  if (!docketPath.startsWith('/api/') || docketPath.includes('..') || docketPath.includes('://')) {
    return jsonSend(500, {error: 'Relay refused an internal path'}, secret);
  }
  const found = discover();
  if (found.error) return jsonSend(503, {error: found.error}, secret);
  const headers = {accept: 'application/json'};
  if (auth) {
    const docketToken = readDocketToken(found.home);
    if (!docketToken) return jsonSend(503, {error: 'Docket token is not available to the relay'}, secret);
    headers.authorization = 'Bearer ' + docketToken;
    secret = docketToken;
  }
  if (method !== 'GET') headers['content-type'] = 'application/json';
  try {
    const response = await fetch(found.url + docketPath, {
      method,
      headers,
      body: method === 'GET' ? undefined : '{}',
      redirect: 'manual',
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (response.status >= 300 && response.status < 400) {
      return jsonSend(502, {error: 'Docket redirected; the relay does not follow redirects'}, secret);
    }
    // Bound bytes before parsing; never relay a raw provider response.
    const reader = response.body?.getReader();
    const chunks = [];
    let size = 0;
    if (!reader) return jsonSend(502, {error: 'Docket response unavailable'}, secret);
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > maxBytes) {
        await reader.cancel();
        return jsonSend(502, {error: 'Docket response exceeded relay bounds'}, secret);
      }
      chunks.push(Buffer.from(value));
    }
    const text = Buffer.concat(chunks).toString('utf8');
    return {status: response.status, body: scrub(text, secret), secret};
  } catch {
    return jsonSend(503, {error: 'Docket is not running on the loopback address in server.json'}, secret);
  }
}

function parseBody(result) {
  try {
    const value = JSON.parse(result.body || '');
    return value && typeof value === 'object' ? value : null;
  } catch {
    return null;
  }
}

const OUTCOMES = new Set(['pending', 'admitted', 'delivered', 'failed', 'uncertain', 'cancelled', 'not_applicable']);
const FACTS = ['admitted', 'claimed', 'injected', 'consumed', 'cancelled'];
const ISSUE_CODES = new Set(['unavailable', 'bad_target', 'archived', 'wrong_target', 'refused', 'conflict', 'acceptance_unknown', 'lost', 'legacy_envelope_unknown', 'legacy_identity_unknown', 'backend_unverifiable', 'backend_changed', 'already_consumed', 'already_admitted', 'cancelled_identity', 'in_flight', 'schedule_advanced', 'schedule_finished', 'retry_budget', 'attempt_budget', 'limits', 'not_ready', 'invalid', 'unsupported_capability', 'invalid_receipt', 'legacy_identity_unverified', 'not_found', 'unauthorized']);
const record = value => value && typeof value === 'object' && !Array.isArray(value);
const safeCode = value => ISSUE_CODES.has(value) ? value : null;
function safeIssue(value) {
  if (!record(value) || !safeCode(value.code)) return null;
  return {code: value.code, detail: clip(value.detail, 500), phase: clip(value.phase, 40), admission_certainty: clip(value.admission_certainty, 40)};
}
function deliveryView(value) {
  if (!record(value) || !OUTCOMES.has(value.outcome) || !record(value.facts)
    || !record(value.recovery) || typeof value.recovery.reconcile !== 'boolean'
    || typeof value.recovery.retry !== 'boolean' || typeof value.stale !== 'boolean'
    || value.execution !== 'unknown' || !(matches(ITEM_ID, value.unit_id) || matches(OCC_ID, value.unit_id))
    || !(value.recovery.refusal_code === null || safeCode(value.recovery.refusal_code))
    || FACTS.some(key => value.facts[key] !== null && typeof value.facts[key] !== 'boolean')
    || !['legacy', 'request'].includes(value.evidence) || typeof value.phase !== 'string') return null;
  return {
    unit_id: value.unit_id,
    request_id: matches(REQUEST_ID, value.request_id) ? value.request_id : null,
    outcome: value.outcome, phase: clip(value.phase, 40), execution: 'unknown',
    facts: Object.fromEntries(FACTS.map(key => [key, value.facts[key]])),
    evidence: ['legacy', 'request'].includes(value.evidence) ? value.evidence : null,
    issue: safeIssue(value.issue), check_issue: safeIssue(value.check_issue), receipt_issue: safeIssue(value.receipt_issue),
    last_checked_at: clip(value.last_checked_at, 50) || null, stale: value.stale,
    namespace_verified: value.namespace_verified === true, same_daemon_recovery: value.same_daemon_recovery === true,
    recovery: {reconcile: value.recovery.reconcile, retry: value.recovery.retry,
      refusal_code: safeCode(value.recovery.refusal_code)},
  };
}
function summaryView(value) {
  if (!record(value)) return null;
  return Object.fromEntries([...OUTCOMES].filter(key => Number.isSafeInteger(value[key]) && value[key] >= 0).map(key => [key, value[key]]));
}

function viewItem(item) {
  const target = item.target && typeof item.target === 'object' ? item.target : {};
  const spec = item.spec && typeof item.spec === 'object' ? item.spec : null;
  const open = item.open_occurrence && typeof item.open_occurrence === 'object' ? item.open_occurrence : null;
  return {
    id: matches(ITEM_ID, item.id) ? item.id : null,
    kind: clip(item.kind, 40),
    status: clip(item.status, 40),
    fire_at: clip(item.fire_at, 50),
    updated_at: clip(item.updated_at, 50),
    fields_truncated: item.fields_truncated === true,
    check: spec && typeof spec.check === 'string' ? clip(spec.check, 80) : null,
    every_seconds: spec && Number.isFinite(spec.every) ? spec.every : null,
    reason: clip(item.reason, 2000),
    urgent: item.urgent === true,
    late: item.late === true,
    late_by_seconds: typeof item.late_by_seconds === 'number' ? item.late_by_seconds : null,
    created_by: clip(item.created_by, 300) || null,
    session_id: clip(target.session_id, 300),
    directory: clip(target.directory, 1000) || null,
    delivery: deliveryView(item.delivery),
    delivery_summary: summaryView(item.delivery_summary),
    coalesced_slots: Number.isSafeInteger(item.coalesced_slots) && item.coalesced_slots >= 0 ? item.coalesced_slots : null,
    // Scope comes from the core open pointer, never from an ID prefix guess.
    unit_scope: ['recurring', 'watch'].includes(item.kind)
      ? (matches(OCC_ID, open?.id) && item.delivery?.unit_id === open.id ? 'occurrences' : null)
      : (item.delivery?.unit_id === item.id ? 'items' : null),
  };
}

function parentView(item) {
  if (!item) return {};
  const target = item.target && typeof item.target === 'object' ? item.target : {};
  return {
    reason: clip(item.reason, 2000),
    created_by: clip(item.created_by, 300) || null,
    session_id: clip(target.session_id, 300),
    directory: clip(target.directory, 1000) || null,
    kind: clip(item.kind, 40),
  };
}

async function snapshot() {
  const found = discover();
  if (found.error) return jsonSend(503, {error: found.error});
  const secret = readDocketToken(found.home);
  const [meta, outstanding, recentItems, occurrences] = await Promise.all([
    docketFetch('GET', '/api/v1/meta', {secret}),
    docketFetch('GET', '/api/v1/items?status=outstanding&view=panel', {secret, maxBytes: panelBytes(200)}),
    docketFetch('GET', '/api/v1/items?status=terminal&view=panel', {secret, maxBytes: panelBytes(HISTORY_LIMIT)}),
    docketFetch('GET', '/api/v1/occurrences?status=all&view=panel', {secret, maxBytes: panelBytes(HISTORY_LIMIT)}),
  ]);
  if (outstanding.status >= 400) {
    return jsonSend(outstanding.status, {
      error: 'Docket did not return outstanding items',
    }, secret);
  }
  const outstandingBody = parseBody(outstanding) || {};
  if (!Array.isArray(outstandingBody.items)) return jsonSend(502, {error: 'Docket list is unavailable'}, secret);
  const validItem = item => record(item) && matches(ITEM_ID, item.id);
  if (outstandingBody.items.some(item => !validItem(item))) return jsonSend(502, {error: 'Docket list contains unavailable records'}, secret);
  const items = outstandingBody.items.slice(0, 200).map(viewItem);
  const recentBody = recentItems.status < 400 ? parseBody(recentItems) : null;
  const historyUnavailable = occurrences.status >= 400;
  const occurrenceBody = occurrences.status < 400 ? parseBody(occurrences) : null;
  const validOccurrence = occ => record(occ) && matches(OCC_ID, occ.id) && matches(ITEM_ID, occ.item_id);
  const rawOccurrences = Array.isArray(occurrenceBody?.occurrences) ? occurrenceBody.occurrences.filter(validOccurrence).slice(0, HISTORY_LIMIT) : [];
  const parents = new Map();
  for (const item of [...outstandingBody.items, ...(Array.isArray(recentBody?.items) ? recentBody.items : [])]) {
    if (validItem(item)) parents.set(item.id, item);
  }
  for (const occ of rawOccurrences) {
    const itemId = occ && occ.item_id;
    if (!matches(ITEM_ID, itemId) || parents.has(itemId)) continue;
    const loaded = await docketFetch('GET', '/api/v1/items/' + itemId + '?view=panel', {secret, maxBytes: panelBytes(1)});
    const body = loaded.status < 400 ? parseBody(loaded) : null;
    if (body && validItem(body.item)) parents.set(itemId, body.item);
  }
  const metaBody = meta.status < 400 ? parseBody(meta) : null;
  const historyItems = Array.isArray(recentBody?.items) ? recentBody.items.filter(validItem).filter(item => !CANCELLABLE.has(item.status)) : [];
  const windowOK = (body, limit, order) => record(body?.window) && body.window.view === 'panel'
    && body.window.limit === limit && body.window.order === order && typeof body.window.truncated === 'boolean';
  const safeWindow = (body, limit, order) => windowOK(body, limit, order)
    ? {view: 'panel', limit, order, truncated: body.window.truncated} : null;
  const itemWindowOK = windowOK(recentBody, HISTORY_LIMIT, 'updated_at_desc,id_desc');
  const occurrenceWindowOK = windowOK(occurrenceBody, HISTORY_LIMIT, 'updated_at_desc,id_desc');
  return jsonSend(200, {
    generated_at: new Date().toISOString(),
    version: metaBody && typeof metaBody.version === 'string' ? clip(metaBody.version, 80) : null,
    history_unavailable: historyUnavailable || !occurrenceWindowOK || !Array.isArray(occurrenceBody?.occurrences) || occurrenceBody.occurrences.some(occ => !validOccurrence(occ)),
    items_history_unavailable: !itemWindowOK || !Array.isArray(recentBody?.items) || recentBody.items.some(item => !validItem(item)),
    history_limited: true,
    history_truncated: recentBody?.window?.truncated === true || occurrenceBody?.window?.truncated === true
      || (occurrenceBody?.occurrences?.length || 0) > HISTORY_LIMIT || historyItems.length > HISTORY_LIMIT,
    outstanding_limited: outstandingBody.window?.truncated === true || outstandingBody.items.length > 200,
    outstanding_window_unavailable: !windowOK(outstandingBody, 200, 'fire_at,id'),
    history_parent_unavailable: rawOccurrences.some(occ => !parents.has(occ.item_id)),
    fields_truncated: [...parents.values(), ...rawOccurrences].some(row => row.fields_truncated === true),
    windows: {outstanding: safeWindow(outstandingBody, 200, 'fire_at,id'),
      items: safeWindow(recentBody, HISTORY_LIMIT, 'updated_at_desc,id_desc'),
      occurrences: safeWindow(occurrenceBody, HISTORY_LIMIT, 'updated_at_desc,id_desc')},
    outstanding: items,
    history_items: historyItems.slice(0, HISTORY_LIMIT).map(viewItem),
    history_occurrences: rawOccurrences.map(occ => {
      const parent = parents.get(occ.item_id) || null;
      return {
        id: occ.id,
        item_id: occ.item_id,
        seq: Number.isSafeInteger(occ.seq) ? occ.seq : null,
        status: clip(occ.status, 40),
        scheduled_at: clip(occ.scheduled_at, 50),
        late: occ.late === true,
        late_by_seconds: typeof occ.late_by_seconds === 'number' ? occ.late_by_seconds : null,
        delivery: deliveryView(occ.delivery),
        unit_scope: occ.delivery?.unit_id === occ.id ? 'occurrences' : null,
        current_unit: parent?.open_occurrence?.id === occ.id && parent?.delivery?.unit_id === occ.id,
        ...parentView(parent),
      };
    }),
  }, secret);
}

async function mutate(scope, id, operation) {
  if (!['items', 'occurrences'].includes(scope) || !(scope === 'items' ? ITEM_ID : OCC_ID).test(id)
    || !['reconcile', 'retry', ...(scope === 'items' ? ['cancel'] : [])].includes(operation)) {
    return jsonSend(400, {error: {code: 'invalid', detail: 'Exact unit and supported operation required'}});
  }
  const result = await docketFetch('POST', `/api/v1/${scope}/${encodeURIComponent(id)}/${operation}`, {auth: true});
  const body = parseBody(result);
  const retraction = ['not_sent', 'retracted', 'too_late', 'uncertain'].includes(body?.retraction?.outcome) ? body.retraction.outcome : null;
  return jsonSend(result.status, {
    operation, authorized: result.status === 202 && body?.authorized === true,
    error: safeIssue(body?.error) || (result.status >= 400 ? {code: 'unavailable', detail: 'Operation refused or unavailable; no automatic resend.'} : null),
    item: record(body?.item) ? viewItem(body.item) : null,
    occurrence: record(body?.occurrence) ? {id: matches(OCC_ID, body.occurrence.id) ? body.occurrence.id : null, delivery: deliveryView(body.occurrence.delivery)} : null,
    schedule_stopped: typeof body?.schedule_stopped === 'boolean' ? body.schedule_stopped : null,
    retraction,
  }, result.secret);
}

function emptyBody(req) {
  return new Promise(resolve => {
    let size = 0;
    let raw = '';
    req.on('data', chunk => {
      size += chunk.length;
      if (size <= 1024) raw += chunk.toString('utf8');
      if (size > 65536) { resolve(false); req.destroy(); }
    });
    req.on('end', () => {
      try {
        const value = raw ? JSON.parse(raw) : {};
        resolve(size <= 1024 && record(value) && Object.keys(value).length === 0);
      } catch { resolve(false); }
    });
    req.on('error', () => resolve(false));
  });
}

const server = http.createServer(async (req, res) => {
  const respond = ({status, body}) => {
    res.writeHead(status, {
      'content-type': 'application/json',
      'cache-control': 'no-store',
    });
    res.end(body);
  };
  if (!authorized(req.headers.authorization)) {
    return respond({status: 401, body: '{"error":"Unauthorized"}'});
  }
  const url = new URL(req.url || '/', 'http://127.0.0.1');
  if (req.method === 'GET' && url.pathname === '/health') {
    return respond({status: 200, body: '{"ready":true}'});
  }
  if (req.method === 'GET' && url.pathname === '/snapshot' && !url.search) {
    return respond(await snapshot());
  }
  const operation = url.pathname.match(/^\/(items|occurrences)\/((?:dkt|occ)_[0-9a-f]{16})\/(remove|reconcile|retry)$/);
  if (req.method === 'POST' && operation && !url.search) {
    if (!await emptyBody(req)) return respond(jsonSend(400, {error: {code: 'invalid', detail: 'Empty object required; overrides are not supported'}}));
    if (operation[3] === 'remove' && operation[1] !== 'items') return respond(jsonSend(400, {error: {code: 'invalid'}}));
    return respond(await mutate(operation[1], operation[2], operation[3] === 'remove' ? 'cancel' : operation[3]));
  }
  respond({status: 404, body: '{"error":"Unknown Docket route"}'});
});

server.listen(port, '127.0.0.1', () => {
  const bound = server.address();
  process.stdout.write(`docket-relay listening ${bound && bound.port}\n`);
});
process.on('SIGTERM', () => server.close(() => process.exit(0)));
