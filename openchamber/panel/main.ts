import {connectHost, HostRequestError} from '@openchamber/sdk';
import {applyHostReady, mountBadge, mountBanner, mountButton, mountEmpty, mountSpinner, mountTabs, type Tone} from '@openchamber/sdk/ui';

type Delivery = {
  unit_id: string; request_id: string | null; outcome: string; phase: string;
  facts: Record<string, boolean | null>; execution: 'unknown'; evidence: string | null;
  stale: boolean; last_checked_at: string | null;
  issue: Issue | null; check_issue: Issue | null; receipt_issue: Issue | null;
  recovery: {reconcile: boolean; retry: boolean; refusal_code: string | null};
  namespace_verified: boolean; same_daemon_recovery: boolean;
};
type Issue = {code: string; detail: string};
type Evidence = {delivery: Delivery | null; unit_scope: 'items' | 'occurrences' | null};
type Item = Evidence & {
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
  delivery_summary: Record<string, number> | null;
  coalesced_slots: number | null;
};

type Occurrence = Evidence & {
  current_unit: boolean;
  id: string;
  item_id: string;
  seq: number;
  status: string;
  scheduled_at: string;
  late: boolean;
  late_by_seconds: number | null;
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
  items_history_unavailable: boolean;
  history_limited: boolean;
  history_truncated: boolean;
  outstanding_limited: boolean;
  outstanding_window_unavailable: boolean;
  history_parent_unavailable: boolean;
  fields_truncated: boolean;
  outstanding: Item[];
  history_items: Item[];
  history_occurrences: Occurrence[];
};

type Notice = {tone: 'info' | 'warning' | 'error' | 'success'; title: string; body?: string};
type View = 'outstanding' | 'undeliverable';

const ITEM_ID = /^dkt_[0-9a-f]{16}$/;
const OCC_ID = /^occ_[0-9a-f]{16}$/;
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

function stateOf(item: Evidence): {key: string; label: string; tone: Tone} {
  const key = item.delivery?.outcome || 'unknown';
  const tone: Tone = key === 'delivered' ? 'success' : key === 'failed' ? 'error'
    : key === 'uncertain' ? 'warning' : key === 'admitted' ? 'info' : 'neutral';
  return {key, label: key === 'unknown' ? 'Delivery unavailable / unknown'
    : key === 'not_applicable' ? 'No open delivery unit' : `Message: ${key}`, tone};
}

function evidence(card: HTMLElement, row: Evidence, historical = false) {
  const d = row.delivery;
  if (!d) {
    card.append(el('div', 'meta', 'Delivery evidence and recovery unavailable (old or malformed backend projection).'));
    return;
  }
  card.append(metaLine([
    d.request_id ? `Request ${d.request_id}` : 'Request identity not available for display',
    `Evidence ${d.evidence || 'unknown'} · phase ${d.phase || 'unknown'}`,
    d.last_checked_at ? `Checked ${when(d.last_checked_at)}` : 'Not checked',
    d.stale ? 'Evidence stale — latest check unavailable' : null,
    d.same_daemon_recovery ? 'Original live-daemon binding retained' : null,
  ]));
  card.append(el('div', 'meta', Object.entries(d.facts).map(([key, value]) => `${key}: ${value === true ? 'yes' : value === false ? 'no' : 'unknown'}`).join(' · ')));
  for (const issue of [d.issue, d.check_issue, d.receipt_issue]) {
    if (issue) card.append(el('div', 'error', `${issue.code}: ${issue.detail}`));
  }
  const actions = el('div', 'actions');
  const pending = busy.has(d.unit_id);
  const canAddress = row.unit_scope !== null;
  mountButton(actions, {label: 'Check status', variant: 'outline', size: 'xs', disabled: pending || !canAddress || !d.recovery.reconcile,
    onClick: () => void recover(row, 'reconcile')});
  mountButton(actions, {label: 'Safe retry', variant: 'outline', size: 'xs', disabled: pending || historical || !canAddress || !d.recovery.retry,
    onClick: () => void recover(row, 'retry', historical)});
  card.append(actions);
  if (!canAddress) card.append(el('div', 'meta', 'Recovery unavailable: no exact current delivery unit.'));
  if (!d.recovery.reconcile) card.append(el('div', 'meta', 'Status check unavailable: no saved request identity.'));
  if (historical || !d.recovery.retry) card.append(el('div', 'meta', `Retry unavailable: ${historical ? 'historical unit (read-only)' : d.recovery.refusal_code || 'core eligibility unavailable'}.`));
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
  mountBadge(flags, {label: `Schedule: ${item.status}`, tone: 'neutral'});
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
  if (item.delivery_summary) card.append(el('div', 'meta', `Series summary (bounded core counts): ${Object.entries(item.delivery_summary).map(([key, count]) => `${key} ${count}`).join(' · ') || 'no occurrences'} · coalesced slots ${item.coalesced_slots ?? 'unknown'} (not delivered wakes)`));
  evidence(card, item);
  if (CANCELLABLE.has(item.status)) {
    const actions = el('div', 'actions');
    mountButton(actions, {
      label: 'Remove',
      variant: 'outline',
      size: 'xs',
      loading: busy.has(item.id),
      disabled: busy.has(item.id),
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
  top.append(el('span', 'kind', kind === 'occurrence' ? 'Wake evidence' : 'Item evidence'));
  mountBadge(top, {label: `Schedule: ${row.status}`, tone: 'neutral'});
  const state = stateOf(row);
  mountBadge(top, {label: state.label, tone: state.tone});
  card.append(top);
  const reason = 'reason' in row ? row.reason : '';
  card.append(el('div', 'reason', reason || '(no reason)'));
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
  if (kind === 'item' && (row as Item).delivery_summary) card.append(el('div', 'meta', `Series summary (bounded core counts): ${Object.entries((row as Item).delivery_summary!).map(([key, count]) => `${key} ${count}`).join(' · ')} · coalesced slots ${(row as Item).coalesced_slots ?? 'unknown'} (not delivered wakes)`));
  evidence(card, row, kind === 'occurrence' ? !(row as Occurrence).current_unit : ['watch', 'recurring'].includes(row.kind || ''));
  return card;
}

function historyRows() {
  if (!snapshot) return [];
  return [
    ...snapshot.history_occurrences.map(row => historyCard(row, 'occurrence')),
    ...snapshot.history_items.map(row => historyCard(row, 'item')),
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
      {id: 'undeliverable', label: 'Recent evidence', count: history.length},
    ],
    activeId: view,
    onChange: id => {
      view = id === 'undeliverable' ? 'undeliverable' : 'outstanding';
      paint();
    },
  });
  if (snapshot.fields_truncated) list.append(el('div', 'meta', 'Some display metadata was shortened by the compact read view.'));
  if (view === 'outstanding' && (snapshot.history_unavailable || snapshot.items_history_unavailable)) list.append(el('div', 'error', 'Some optional history is unavailable; outstanding work is still shown.'));
  if (view === 'outstanding') {
    if (!outstanding.length) {
      mountEmpty(list, {title: 'Nothing outstanding', body: 'No pending, firing, admitted, or ambiguous item.'});
    }
    for (const item of outstanding) list.append(itemCard(item));
    if (snapshot.outstanding_limited) list.append(el('div', 'meta', 'Outstanding list truncated by relay bounds.'));
    if (snapshot.outstanding_window_unavailable) list.append(el('div', 'error', 'Outstanding window completeness is unavailable on this backend.'));
  } else {
    if (snapshot.history_unavailable || snapshot.items_history_unavailable) list.append(el('div', 'error', 'Some history is unavailable; this is not evidence of no delivery.'));
    if (snapshot.history_parent_unavailable) list.append(el('div', 'error', 'Some history parent details are unavailable.'));
    list.append(el('div', 'meta', `Bounded recent evidence by last update, not a complete audit. Core and panel: at most 40 terminal items and 40 occurrences.${snapshot.history_truncated ? ' Results truncated.' : ''}`));
    if (!history.length) mountEmpty(list, {title: 'No evidence available in this window', body: 'Absence is not proof that a message was not sent.'});
    for (const row of history) list.append(row);
  }
  const version = snapshot.version ? `Service ${snapshot.version}. ` : '';
  node('footer').textContent = version + 'Delivered means matching transcript consumption, not successful agent execution (unknown). Coalesced slots are not delivered wakes. The panel does not restart the service.';
}

function say(next: Notice | null) {
  message = next;
  clearTimeout(messageTimer);
  if (next) messageTimer = window.setTimeout(() => { message = null; paint(); }, 30_000);
  paint();
}

function explain(error: unknown): Notice {
  if (error instanceof HostRequestError) {
    if (error.code === 'NO_SERVICE') return {tone: 'warning', title: 'Allow the Docket service', body: 'Approve it in Settings → Extensions. It relays lists, stop, status checks and core-authorized recovery to Docket on loopback.'};
    if (error.code === 'DISABLED') return {tone: 'info', title: 'Docket is paused', body: 'Resume it in Settings → Extensions.'};
    if (error.code === 'SERVICE_FAILED') return {tone: 'error', title: 'The Docket relay stopped', body: 'Close and reopen this panel to restart it.'};
  }
  return {tone: 'error', title: 'Docket is unreachable', body: 'Operation unavailable or timed out. No automatic resend; refresh or check status.'};
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
  if (busy.has(id)) return;
  if (!ITEM_ID.test(id)) {
    say({tone: 'error', title: 'Not removed', body: 'That is not a Docket id.'});
    return;
  }
  busy.add(id);
  paint();
  try {
    const {status, value} = await call('POST', `/items/${id}/remove`);
    const stopped = value.schedule_stopped === true;
    const retraction = value.retraction;
    const coreError = value.error as Issue | null;
    const details = retraction === 'not_sent' ? 'Core confirms this wake was not sent.'
      : retraction === 'retracted' ? 'Core confirms retraction.'
      : retraction === 'too_late' ? 'Already consumed; stopping the schedule does not undo delivery or establish execution.'
      : 'Prior delivery is uncertain; an in-flight or late wake may still arrive. Check status.';
    say({tone: status === 200 && stopped && ['not_sent', 'retracted'].includes(String(retraction)) ? 'success' : 'warning',
      title: status === 200 && stopped ? 'Schedule stopped' : 'Stop not confirmed',
      body: `${stopped ? 'Schedule is stopped. ' : ''}${details}${coreError ? ` ${coreError.code}: ${coreError.detail}` : ''}`});
    const returned = value.item as Item | null;
    if (returned?.id === id && snapshot) {
      snapshot.outstanding = snapshot.outstanding.map(item => item.id === id ? returned : item);
    }
  } catch (error) {
    say({...explain(error), title: 'Could not confirm removal'});
  }
  busy.delete(id);
  await load();
}

async function recover(row: Evidence, operation: 'reconcile' | 'retry', historical = false) {
  const d = row.delivery;
  const scope = row.unit_scope;
  if (!d || !scope || !(scope === 'items' ? ITEM_ID : OCC_ID).test(d.unit_id)
    || busy.has(d.unit_id) || !d.recovery[operation] || (operation === 'retry' && historical)) return;
  busy.add(d.unit_id);
  paint();
  try {
    const {status, value} = await call('POST', `/${scope}/${encodeURIComponent(d.unit_id)}/${operation}`);
    const current = value.occurrence || value.item;
    const result = current as {delivery?: Delivery} | null;
    const error = value.error as Issue | null;
    if (operation === 'retry' && status === 202 && value.authorized === true) {
      say({tone: 'info', title: 'Retry authorized — not delivered', body: 'Core authorized one same-unit attempt. The scheduler performs it later; no new identity or automatic client resend.'});
    } else if (operation === 'reconcile' && status === 200 && result?.delivery) {
      say({tone: 'info', title: 'Status checked', body: `Core outcome: ${result.delivery.outcome}. Execution remains unknown.`});
    } else {
      say({tone: 'warning', title: `Operation refused or unavailable (${status})`,
        body: error ? `${error.code}: ${error.detail}${result?.delivery ? ` · Core outcome: ${result.delivery.outcome}` : ''}` : 'No confirmed operation result. No automatic resend.'});
    }
    // Retain returned evidence even when the following refresh is unavailable.
    const returned = result?.delivery;
    if (returned?.unit_id === d.unit_id) row.delivery = returned;
  } catch {
    say({tone: 'warning', title: 'Operation result unknown', body: 'Timed out or unavailable. No automatic resend; use a status check before further action.'});
  } finally {
    busy.delete(d.unit_id);
    await load();
  }
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
