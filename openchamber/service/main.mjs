// Loopback relay from the OpenChamber panel to Docket.
//
// The panel runs in a sandboxed iframe and cannot reach 127.0.0.1. OpenChamber
// starts this process and forwards serviceRequest calls here. This process
// reads the Docket token from disk and attaches it on the way to Docket.
// The token is never written to a response, a log line, or the panel.
//
// The panel does not choose the backend. The URL comes from DOCKET_URL, only
// when that value is loopback, or otherwise from server.json under DOCKET_HOME
// or ~/.docket. OpenCode's port is not used here; Docket discovers that itself.
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
const TIMEOUT_MS = 8000;
const HISTORY_LIMIT = 40;
const CANCELLABLE = new Set(['pending', 'firing', 'admitted', 'ambiguous']);

const DETAILS = {
  consumed: 'Already delivered. This delivery was already consumed by the session.',
  undeliverable: 'Not removed. It is already undeliverable.',
  cancelled: 'Item already stopped. This does not confirm that an earlier wake was retracted.',
  expired: 'Not removed. It already expired.',
  missing: 'Not removed. No such item.',
  terminal: 'Not removed. It is no longer pending.',
  rejected: 'Not removed. That is not a Docket id.',
};

function docketHome() {
  if (process.env.DOCKET_HOME) return path.resolve(process.env.DOCKET_HOME);
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
  return value.length > max ? value.slice(0, max) : value;
}

function jsonSend(status, payload, secret) {
  const encoded = JSON.stringify(payload);
  return {status, body: scrub(encoded, secret)};
}

async function docketFetch(method, docketPath, {auth = false, secret = ''} = {}) {
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
    const text = await response.text();
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

function deliveryState(item) {
  if (!item || typeof item !== 'object') return null;
  if (typeof item.delivery_state === 'string') return item.delivery_state;
  const receipt = item.receipt;
  if (receipt && typeof receipt === 'object' && typeof receipt.state === 'string') return receipt.state;
  const open = item.open_occurrence;
  if (open && typeof open === 'object' && typeof open.delivery_state === 'string') return open.delivery_state;
  return null;
}

function outcomeOf(item, missing = false) {
  if (missing || !item) return 'missing';
  const state = deliveryState(item);
  const status = item.status;
  if (state === 'consumed' || status === 'delivered' || status === 'completed') return 'consumed';
  if (status === 'cancelled') return 'cancelled';
  if (status === 'undeliverable' || state === 'lost') return 'undeliverable';
  if (status === 'expired') return 'expired';
  return 'terminal';
}

function viewItem(item) {
  const target = item.target && typeof item.target === 'object' ? item.target : {};
  const spec = item.spec && typeof item.spec === 'object' ? item.spec : null;
  const open = item.open_occurrence && typeof item.open_occurrence === 'object' ? item.open_occurrence : null;
  return {
    id: item.id,
    kind: item.kind,
    status: item.status,
    fire_at: item.fire_at,
    check: spec && typeof spec.check === 'string' ? spec.check : null,
    every_seconds: spec && Number.isFinite(spec.every) ? spec.every : null,
    reason: clip(item.reason, 2000),
    urgent: item.urgent === true,
    late: item.late === true,
    late_by_seconds: typeof item.late_by_seconds === 'number' ? item.late_by_seconds : null,
    created_by: typeof item.created_by === 'string' ? item.created_by : null,
    session_id: typeof target.session_id === 'string' ? target.session_id : '',
    directory: typeof target.directory === 'string' ? target.directory : null,
    delivery_state: deliveryState(item),
    open_status: open && typeof open.status === 'string' ? open.status : null,
    error: typeof item.error === 'string' ? clip(item.error, 500) : null,
  };
}

function parentView(item) {
  if (!item) return {};
  const target = item.target && typeof item.target === 'object' ? item.target : {};
  return {
    reason: clip(item.reason, 2000),
    created_by: typeof item.created_by === 'string' ? item.created_by : null,
    session_id: typeof target.session_id === 'string' ? target.session_id : '',
    directory: typeof target.directory === 'string' ? target.directory : null,
    kind: item.kind,
  };
}

async function snapshot() {
  const found = discover();
  if (found.error) return jsonSend(503, {error: found.error});
  const secret = readDocketToken(found.home);
  const [meta, outstanding, undeliverableItems, occurrences] = await Promise.all([
    docketFetch('GET', '/api/v1/meta', {secret}),
    docketFetch('GET', '/api/v1/items?status=outstanding', {secret}),
    docketFetch('GET', '/api/v1/items?status=undeliverable', {secret}),
    docketFetch('GET', '/api/v1/occurrences?status=undeliverable', {secret}),
  ]);
  if (outstanding.status >= 400) {
    const parsed = parseBody(outstanding);
    const detail = parsed && (parsed.error?.detail || parsed.error);
    return jsonSend(outstanding.status, {
      error: typeof detail === 'string' ? detail : 'Docket did not return outstanding items',
    }, secret);
  }
  const outstandingBody = parseBody(outstanding) || {};
  const items = Array.isArray(outstandingBody.items) ? outstandingBody.items.map(viewItem) : [];
  const failedItemsBody = undeliverableItems.status < 400 ? parseBody(undeliverableItems) : null;
  const failedItems = Array.isArray(failedItemsBody?.items) ? failedItemsBody.items.map(viewItem) : [];
  const historyUnavailable = occurrences.status === 404;
  const occurrenceBody = occurrences.status < 400 ? parseBody(occurrences) : null;
  const rawOccurrences = Array.isArray(occurrenceBody?.occurrences) ? occurrenceBody.occurrences.slice(0, HISTORY_LIMIT) : [];
  const parents = new Map();
  for (const item of [...(Array.isArray(outstandingBody.items) ? outstandingBody.items : []), ...(failedItemsBody?.items || [])]) {
    if (item && ITEM_ID.test(item.id || '')) parents.set(item.id, item);
  }
  for (const occ of rawOccurrences) {
    const itemId = occ && occ.item_id;
    if (!ITEM_ID.test(itemId || '') || parents.has(itemId)) continue;
    const loaded = await docketFetch('GET', '/api/v1/items/' + itemId, {secret});
    const body = loaded.status < 400 ? parseBody(loaded) : null;
    if (body && body.item) parents.set(itemId, body.item);
  }
  const occurrenceIds = new Set(rawOccurrences.map(occ => occ && occ.item_id).filter(Boolean));
  const metaBody = meta.status < 400 ? parseBody(meta) : null;
  return jsonSend(200, {
    generated_at: new Date().toISOString(),
    version: metaBody && typeof metaBody.version === 'string' ? metaBody.version : null,
    history_unavailable: historyUnavailable,
    outstanding: items,
    undeliverable_items: failedItems.filter(item => !occurrenceIds.has(item.id)),
    undeliverable_occurrences: rawOccurrences.map(occ => {
      const parent = parents.get(occ.item_id) || null;
      return {
        id: occ.id,
        item_id: occ.item_id,
        seq: occ.seq,
        status: occ.status,
        scheduled_at: occ.scheduled_at,
        late: occ.late === true,
        late_by_seconds: typeof occ.late_by_seconds === 'number' ? occ.late_by_seconds : null,
        error: typeof occ.error === 'string' ? clip(occ.error, 500) : null,
        ...parentView(parent),
      };
    }),
  }, secret);
}

async function removeItem(id) {
  if (!ITEM_ID.test(id)) return jsonSend(400, {removed: false, outcome: 'rejected', detail: DETAILS.rejected});
  const found = discover();
  if (found.error) return jsonSend(503, {removed: false, outcome: 'unavailable', detail: found.error});
  const secret = readDocketToken(found.home);
  const current = await docketFetch('GET', '/api/v1/items/' + id, {secret});
  if (current.status === 404) {
    return jsonSend(404, {removed: false, outcome: 'missing', detail: DETAILS.missing, id}, secret);
  }
  const before = parseBody(current);
  const item = before && before.item;
  if (current.status >= 400 || !item) {
    return jsonSend(current.status || 502, {
      removed: false,
      outcome: 'unavailable',
      detail: 'Docket did not return this item',
      id,
    }, secret);
  }
  const beforeState = deliveryState(item);
  if (beforeState === 'consumed' || item.status === 'delivered' || item.status === 'completed') {
    return jsonSend(409, {
      removed: false,
      outcome: 'consumed',
      detail: DETAILS.consumed,
      id,
      status: item.status,
      delivery_state: beforeState,
    }, secret);
  }
  if (!CANCELLABLE.has(item.status)) {
    const outcome = outcomeOf(item);
    return jsonSend(409, {
      removed: false,
      outcome,
      detail: DETAILS[outcome] || DETAILS.terminal,
      id,
      status: item.status,
      delivery_state: beforeState,
    }, secret);
  }
  const cancelled = await docketFetch('POST', '/api/v1/items/' + id + '/cancel', {auth: true, secret});
  const body = parseBody(cancelled);
  const retraction = body?.retraction?.outcome;
  const stopped = cancelled.status === 200 && body?.item?.status === 'cancelled';
  if (stopped && (retraction === 'retracted' || retraction === 'none')) {
    return jsonSend(200, {
      removed: true,
      outcome: 'removed',
      retraction,
      detail: 'Removed.',
      id,
      status: 'cancelled',
    }, cancelled.secret || secret);
  }
  // A cancelled row alone does not prove host retraction. In particular,
  // missing/lost stop retries but cannot undo an invisible in-flight wake.
  // Keep refusal status/outcome instead of reclassifying an outstanding retry
  // as terminal after a second GET. The panel refreshes the full list itself.
  let outcome = 'unconfirmed';
  let detail = 'Could not confirm removal. Refresh to check outstanding items; retries may remain.';
  if (retraction === 'consumed' || body?.error?.code === 'consumed') {
    outcome = 'consumed';
    detail = DETAILS.consumed;
  } else if (stopped && retraction === 'missing') {
    outcome = 'missing';
    detail = 'Item stopped, but an in-flight wake may still arrive.';
  } else if (stopped && retraction === 'lost') {
    outcome = 'lost';
    detail = 'Item stopped; its earlier delivery could not be found.';
  } else if (body?.error?.code === 'retraction_failed' || retraction === 'failed') {
    outcome = 'retraction_failed';
    detail = 'Could not confirm removal. The host still holds this admission; the item remains outstanding.';
  } else if (retraction === 'ambiguous' || retraction === 'unreadable') {
    outcome = 'unreadable';
  }
  return jsonSend(cancelled.status, {
    removed: false,
    outcome,
    detail,
    retraction: typeof retraction === 'string' ? retraction : null,
    id,
    status: body?.item?.status || null,
  }, cancelled.secret || secret);
}

function discard(req) {
  return new Promise(resolve => {
    let size = 0;
    req.on('data', chunk => {
      size += chunk.length;
      if (size > 65536) req.destroy();
    });
    req.on('end', resolve);
    req.on('error', resolve);
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
  if (req.method === 'GET' && url.pathname === '/snapshot') {
    return respond(await snapshot());
  }
  const remove = url.pathname.match(/^\/items\/(dkt_[0-9a-f]{16})\/remove$/);
  if (req.method === 'POST' && remove) {
    await discard(req);
    return respond(await removeItem(remove[1]));
  }
  await discard(req);
  respond({status: 404, body: '{"error":"Unknown Docket route"}'});
});

server.listen(port, '127.0.0.1', () => {
  const bound = server.address();
  process.stdout.write(`docket-relay listening ${bound && bound.port}\n`);
});
process.on('SIGTERM', () => server.close(() => process.exit(0)));
