import {connectHost, HostRequestError} from '@openchamber/sdk';
import {applyHostReady, mountBadge, mountBanner, mountButton, mountEmpty, mountSpinner, mountTabs, type Tone} from '@openchamber/sdk/ui';

type Item = {
  id: string;
  kind: string;
  status: string;
  fire_at: string;
  check: string | null;
  every_seconds: number | null;
  reason: string;
  urgent: boolean;
  late: boolean;
  late_by_seconds: number | null;
  created_by: string | null;
  session_id: string;
  directory: string | null;
  delivery_state: string | null;
  open_status: string | null;
  error: string | null;
};

type Occurrence = {
  id: string;
  item_id: string;
  seq: number;
  status: string;
  scheduled_at: string;
  late: boolean;
  late_by_seconds: number | null;
  error: string | null;
  reason?: string;
  created_by?: string | null;
  session_id?: string;
  directory?: string | null;
  kind?: string;
};

type Snapshot = {
  generated_at: string;
  version: string | null;
  history_unavailable: boolean;
  outstanding: Item[];
  undeliverable_items: Item[];
  undeliverable_occurrences: Occurrence[];
};

type Notice = {tone: 'info' | 'warning' | 'error' | 'success'; title: string; body?: string};
type View = 'outstanding' | 'undeliverable';

const ITEM_ID = /^dkt_[0-9a-f]{16}$/;
const VISIBLE_POLL_MS = 30_000;
const HIDDEN_POLL_MS = 120_000;
const CANCELLABLE = new Set(['pending', 'firing', 'admitted', 'ambiguous']);

const host = connectHost();
const node = (id: string) => document.getElementById(id)!;
let snapshot: Snapshot | null = null;
let visible = true;
let timer = 0;
let badge: number | null = null;
let view: View = 'outstanding';
let problem: Notice | null = null;
let message: Notice | null = null;
let messageTimer = 0;
const busy = new Set<string>();

function el<K extends keyof HTMLElementTagNameMap>(tag: K, className = '', text = '') {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}

function when(iso: string | null) {
  if (!iso) return 'not set';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const minutes = Math.round((date.getTime() - Date.now()) / 60_000);
  const clock = date.toLocaleString([], {weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'});
  if (minutes > 1) return `${clock} (in ${minutes}m)`;
  if (minutes < -1) return `${clock} (${-minutes}m ago)`;
  return clock;
}

function lateText(late: boolean, seconds: number | null) {
  if (!late) return '';
  if (seconds == null) return 'late';
  const rounded = Math.round(seconds);
  return rounded === 1 ? 'late by 1s' : `late by ${rounded}s`;
}

function stateOf(item: Item): {key: string; label: string; tone: Tone} {
  const delivery = item.delivery_state;
  const status = item.open_status || item.status;
  if (delivery === 'consumed' || item.status === 'delivered' || item.status === 'completed') {
    return {key: 'consumed', label: 'consumed', tone: 'success'};
  }
  if (item.status === 'undeliverable' || delivery === 'lost') {
    return {key: 'undeliverable', label: 'undeliverable', tone: 'error'};
  }
  if (status === 'admitted' || delivery === 'admitted') {
    return {key: 'admitted', label: 'admitted, not consumed', tone: 'info'};
  }
  if (status === 'ambiguous' || delivery === 'ambiguous') {
    return {key: 'ambiguous', label: 'ambiguous, not retried', tone: 'warning'};
  }
  if (status === 'firing' || delivery === 'sending') {
    return {key: 'firing', label: 'firing', tone: 'warning'};
  }
  return {key: 'pending', label: 'pending', tone: 'neutral'};
}

function metaLine(parts: Array<string | null | undefined>) {
  const line = el('div', 'meta');
  for (const part of parts) {
    if (!part) continue;
    line.append(el('span', '', part));
  }
  return line;
}

function itemCard(item: Item) {
  const state = stateOf(item);
  const card = el('section', 'card');
  card.dataset.itemId = item.id;
  card.dataset.state = state.key;
  const top = el('div', 'top');
  const title = el('span', 'kind', item.check ? `${item.kind} · ${item.check}` : item.kind);
  top.append(title);
  const flags = el('span', 'actions');
  mountBadge(flags, {label: state.label, tone: state.tone});
  if (item.urgent) mountBadge(flags, {label: 'urgent', tone: 'warning'});
  const late = lateText(item.late, item.late_by_seconds);
  if (late) mountBadge(flags, {label: late, tone: 'warning'});
  top.append(flags);
  card.append(top);
  card.append(el('div', 'reason', item.reason || '(no reason)'));
  const nextLabel = item.check ? 'Next check' : 'Next fire';
  card.append(metaLine([
    `Set by ${item.created_by || 'not recorded'}`,
    `Session ${item.session_id || 'none'}`,
    item.directory ? `Project ${item.directory}` : null,
    `${nextLabel} ${when(item.fire_at)}`,
    item.every_seconds ? `Poll every ${item.every_seconds}s` : null,
    item.id,
  ]));
  if (item.error) card.append(el('div', 'error', item.error));
  if (CANCELLABLE.has(item.status)) {
    const actions = el('div', 'actions');
    mountButton(actions, {
      label: 'Remove',
      variant: 'outline',
      size: 'xs',
      loading: busy.has(item.id),
      onClick: () => void remove(item.id),
    });
    card.append(actions);
  }
  return card;
}

function historyCard(row: Occurrence | Item, kind: 'occurrence' | 'item') {
  const card = el('section', 'card');
  card.dataset.historyId = kind === 'occurrence' ? (row as Occurrence).id : row.id;
  const top = el('div', 'top');
  top.append(el('span', 'kind', kind === 'occurrence' ? 'Undeliverable wake' : 'Undeliverable item'));
  mountBadge(top, {label: 'undeliverable', tone: 'error'});
  card.append(top);
  const reason = 'reason' in row ? row.reason : '';
  card.append(el('div', 'reason', reason || '(no reason)'));
  const error = row.error;
  if (error) card.append(el('div', 'error', error));
  const whenAt = kind === 'occurrence' ? (row as Occurrence).scheduled_at : (row as Item).fire_at;
  const late = lateText(row.late, row.late_by_seconds);
  card.append(metaLine([
    `Set by ${row.created_by || 'not recorded'}`,
    `Session ${row.session_id || 'none'}`,
    row.directory ? `Project ${row.directory}` : null,
    `Due ${when(whenAt)}`,
    late || null,
    kind === 'occurrence' ? `Item ${(row as Occurrence).item_id}` : row.id,
  ]));
  return card;
}

function historyRows() {
  if (!snapshot) return [];
  return [
    ...snapshot.undeliverable_occurrences.map(row => historyCard(row, 'occurrence')),
    ...snapshot.undeliverable_items.map(row => historyCard(row, 'item')),
  ];
}

function paint() {
  const box = node('notice');
  box.replaceChildren();
  for (const notice of [problem, message]) if (notice) mountBanner(box, notice);
  node('loading').replaceChildren();
  node('tabs').replaceChildren();
  const list = node('list');
  list.replaceChildren();
  if (!snapshot) {
    if (!problem) mountSpinner(node('loading'), {label: 'Reading Docket…'});
    return;
  }
  node('stamp').textContent = 'as of ' + new Date(snapshot.generated_at).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
  const outstanding = snapshot.outstanding;
  const history = historyRows();
  mountTabs(node('tabs'), {
    items: [
      {id: 'outstanding', label: 'Outstanding', count: outstanding.length},
      {id: 'undeliverable', label: 'Undeliverable', count: history.length},
    ],
    activeId: view,
    onChange: id => {
      view = id === 'undeliverable' ? 'undeliverable' : 'outstanding';
      paint();
    },
  });
  if (view === 'outstanding') {
    if (!outstanding.length) {
      mountEmpty(list, {title: 'Nothing outstanding', body: 'No pending, firing, admitted, or ambiguous item.'});
    }
    for (const item of outstanding) list.append(itemCard(item));
  } else if (snapshot.history_unavailable) {
    mountEmpty(list, {
      title: 'History needs the upgraded service',
      body: 'This Docket process does not list undeliverable wakes yet. The running service is upgraded separately.',
    });
  } else if (!history.length) {
    mountEmpty(list, {title: 'No undeliverable wakes', body: 'Failed deliveries stay here after the item leaves the outstanding list.'});
  } else {
    for (const row of history) list.append(row);
  }
  const version = snapshot.version ? `Service ${snapshot.version}. ` : '';
  node('footer').textContent = version + 'The installed Docket service is upgraded separately. This panel does not restart it.';
}

function say(next: Notice | null) {
  message = next;
  clearTimeout(messageTimer);
  if (next) messageTimer = window.setTimeout(() => { message = null; paint(); }, 30_000);
  paint();
}

function explain(error: unknown): Notice {
  if (error instanceof HostRequestError) {
    if (error.code === 'NO_SERVICE') return {tone: 'warning', title: 'Allow the Docket service', body: 'Approve it in Settings → Extensions. It only relays list and remove to Docket on loopback.'};
    if (error.code === 'DISABLED') return {tone: 'info', title: 'Docket is paused', body: 'Resume it in Settings → Extensions.'};
    if (error.code === 'SERVICE_FAILED') return {tone: 'error', title: 'The Docket relay stopped', body: 'Close and reopen this panel to restart it.'};
  }
  return {tone: 'error', title: 'Docket is unreachable', body: error instanceof Error ? error.message : String(error)};
}

async function call(method: 'GET' | 'POST', path: string) {
  const result = await host.serviceRequest({method, path});
  let value: Record<string, unknown> = {};
  try {
    const parsed = JSON.parse(result.body || '{}');
    if (parsed && typeof parsed === 'object') value = parsed;
  } catch {
    value = {};
  }
  return {status: result.status, value};
}

async function load() {
  try {
    const {status, value} = await call('GET', '/snapshot');
    if (status >= 400) {
      const detail = typeof value.error === 'string' ? value.error : `Docket relay answered ${status}`;
      throw new Error(detail);
    }
    snapshot = value as unknown as Snapshot;
    problem = null;
    const count = visible ? null : (snapshot.outstanding?.length || null);
    if (count !== badge) {
      badge = count;
      void host.setBadge(count).catch(() => {});
    }
  } catch (error) {
    problem = explain(error);
  }
  paint();
}

async function remove(id: string) {
  if (!ITEM_ID.test(id)) {
    say({tone: 'error', title: 'Not removed', body: 'That is not a Docket id.'});
    return;
  }
  busy.add(id);
  paint();
  try {
    const {status, value} = await call('POST', `/items/${id}/remove`);
    const detail = typeof value.detail === 'string' ? value.detail : 'Docket did not remove it.';
    if (status === 200 && value.removed === true && value.outcome === 'removed'
      && (value.retraction === 'retracted' || value.retraction === 'none')) {
      say({tone: 'success', title: 'Removed', body: detail});
    }
    else say({
      tone: ['consumed', 'missing', 'lost'].includes(String(value.outcome)) ? 'warning' : 'error',
      title: value.outcome === 'consumed' ? 'Already delivered'
        : status === 200 && ['missing', 'lost'].includes(String(value.outcome)) ? 'Item stopped'
        : 'Could not confirm removal',
      body: detail,
    });
  } catch (error) {
    say({...explain(error), title: 'Could not confirm removal'});
  }
  busy.delete(id);
  await load();
}

function schedule() {
  clearInterval(timer);
  timer = window.setInterval(() => void load(), visible ? VISIBLE_POLL_MS : HIDDEN_POLL_MS);
}

function setVisible(next: boolean) {
  if (next === visible) return;
  visible = next;
  schedule();
  if (visible) void load();
}

let started = false;
host.onReady(context => {
  try {
    applyHostReady(context, document.documentElement);
  } catch {
    // A partial theme must not stop the list from loading.
  }
  if (started) return;
  started = true;
  mountButton(node('refresh'), {label: 'Refresh', variant: 'outline', size: 'xs', onClick: () => void load()});
  paint();
  void load();
  schedule();
});

new IntersectionObserver(entries => setVisible(entries.some(entry => entry.isIntersecting))).observe(document.documentElement);
document.addEventListener('visibilitychange', () => setVisible(!document.hidden));
