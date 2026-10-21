// Headless stand-in for OpenChamber's host proxy.
// The panel iframe uses the real SDK serviceRequest channel. This process
// forwards those calls to the real relay and never receives the Docket token.

import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const panelDir = path.join(root, 'panel');
const chromePath = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const relay = process.env.DOCKET_RELAY;
const serviceToken = process.env.OPENCHAMBER_SERVICE_TOKEN;
const docketToken = process.env.DOCKET_TOKEN_EXPECT || '';
const dbPath = process.env.DOCKET_DB;
const screenshot = process.env.DOCKET_SCREENSHOT;
const userData = process.env.DOCKET_CHROME_DATA;
const debugPort = process.env.DOCKET_DEBUG_PORT;
const feedbackCases = JSON.parse(process.env.DOCKET_FEEDBACK_CASES || 'null');

if (!relay || !serviceToken || !dbPath || !screenshot || !userData || !debugPort) {
  throw new Error('browser proof is missing relay, token, database, or chrome settings');
}
if (!fs.existsSync(chromePath)) {
  console.log('PANEL_RESULT ' + JSON.stringify({ok: false, unsupported: 'Google Chrome is not installed'}));
  process.exit(2);
}

const CHANNEL = 'openchamber.sdk';
const tokens = {
  background: '#1b1b1f', elevated: '#242428', foreground: '#f4f4f5', muted: '#a1a1aa',
  subtle: '#71717a', border: '#3f3f46', hover: '#2a2a2e', selection: '#3f3f46', focus: '#a1a1aa',
  primary: '#e4e4e7', mutedSurface: '#27272a', elevatedForeground: '#f4f4f5', active: '#3f3f46',
  selectionForeground: '#f4f4f5', primaryForeground: '#18181b', primaryText: '#e4e4e7',
  successText: '#86efac', warningText: '#fcd34d', errorText: '#fca5a5', infoText: '#93c5fd',
  success: '#166534', warning: '#854d0e', error: '#991b1b', info: '#1e3a8a',
  font: 'system-ui, sans-serif', mono: 'ui-monospace, monospace', radius: '8px',
};
const readyPayload = {
  theme: {mode: 'dark', tokens},
  locale: 'en',
  directory: null,
  session: null,
  surface: 'panel',
  connection: {connected: true, account: ''},
  settings: {},
  item: null,
};

const harness = `<!doctype html>
<html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:#1b1b1f}iframe{border:0;width:100%;height:100%}
</style></head><body>
<iframe id="panel" title="Docket"></iframe>
<script>
const CHANNEL = ${JSON.stringify(CHANNEL)};
const readyPayload = ${JSON.stringify(readyPayload)};
const calls = [];
window.__calls = calls;
window.__boot = function() {
  const frame = document.getElementById('panel');
  window.addEventListener('message', async (event) => {
    if (event.source !== frame.contentWindow) return;
    const message = event.data;
    if (!message || message.channel !== CHANNEL || message.v !== 1) return;
    if (message.type === 'hello') {
      frame.contentWindow.postMessage({channel: CHANNEL, v: 1, type: 'ready', payload: readyPayload}, '*');
      return;
    }
    if (!message.id) return;
    if (message.type !== 'service-request') {
      frame.contentWindow.postMessage({channel: CHANNEL, v: 1, type: 'result', id: message.id, ok: true}, '*');
      return;
    }
    const method = message.payload && message.payload.method;
    const requestPath = message.payload && message.payload.path;
     calls.push({method, path: requestPath});
     if (method === 'POST' && window.__nextReply) {
       const reply = window.__nextReply;
       window.__nextReply = null;
       frame.contentWindow.postMessage({channel: CHANNEL, v: 1, type: 'result', id: message.id, ...reply}, '*');
       return;
     }
    try {
      const response = await fetch('/oc-host' + requestPath, {
        method,
        headers: {accept: 'application/json'},
        body: method === 'GET' ? undefined : '{}',
      });
      const body = await response.text();
      frame.contentWindow.postMessage({
        channel: CHANNEL, v: 1, type: 'result', id: message.id, ok: true,
        payload: {status: response.status, body},
      }, '*');
    } catch (error) {
      frame.contentWindow.postMessage({
        channel: CHANNEL, v: 1, type: 'result', id: message.id, ok: false,
        code: 'REQUEST_FAILED', error: String(error && error.message || error),
      }, '*');
    }
  });
  frame.src = '/panel/index.html';
};
window.__text = () => document.getElementById('panel').contentDocument.body.innerText;
window.__html = () => document.getElementById('panel').contentDocument.documentElement.innerHTML;
window.__clickRemove = (id) => {
  const card = document.getElementById('panel').contentDocument.querySelector('[data-item-id="' + id + '"]');
  if (!card) return 'missing-card';
  const button = [...card.querySelectorAll('button')].find(node => node.textContent.trim() === 'Remove');
  if (!button) return 'missing-button';
  button.click();
  return 'clicked';
};
window.__openHistory = () => {
  const tab = document.getElementById('panel').contentDocument.querySelector('[role=tab][data-id=undeliverable]');
  if (!tab) return 'missing-tab';
  tab.click();
  return 'clicked';
};
</script></body></html>`;

function serveStatic() {
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url || '/', 'http://127.0.0.1');
    // Same-origin stand-in for OpenChamber's host proxy. The page cannot call
    // the relay directly (no CORS, and the iframe must not hold either token).
    if (url.pathname.startsWith('/oc-host/')) {
      const target = relay + url.pathname.slice('/oc-host'.length);
      try {
        const response = await fetch(target, {
          method: req.method,
          headers: {authorization: 'Bearer ' + serviceToken, accept: 'application/json'},
          body: req.method === 'GET' ? undefined : '{}',
          signal: AbortSignal.timeout(8000),
        });
        const body = await response.text();
        res.writeHead(response.status, {'content-type': 'application/json', 'cache-control': 'no-store'});
        res.end(body);
      } catch (error) {
        res.writeHead(502, {'content-type': 'application/json'});
        res.end(JSON.stringify({error: 'relay proxy failed'}));
      }
      return;
    }
    if (url.pathname === '/harness.html') {
      res.writeHead(200, {'content-type': 'text/html'});
      res.end(harness);
      return;
    }
    if (url.pathname.startsWith('/panel/')) {
      const file = path.normalize(path.join(panelDir, url.pathname.slice('/panel/'.length)));
      if (!file.startsWith(panelDir) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
        res.writeHead(404);
        res.end('missing');
        return;
      }
      const type = file.endsWith('.js') ? 'text/javascript' : 'text/html';
      res.writeHead(200, {'content-type': type});
      res.end(fs.readFileSync(file));
      return;
    }
    res.writeHead(404);
    res.end('missing');
  });
  return new Promise(resolve => {
    server.listen(0, '127.0.0.1', () => resolve(server));
  });
}

function send(ws, pending, method, params = {}, sessionId) {
  const id = pending.next++;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.map.delete(id);
      reject(new Error(`CDP timeout ${method}`));
    }, 15000);
    pending.map.set(id, {resolve, reject, timer});
    const message = {id, method, params};
    if (sessionId) message.sessionId = sessionId;
    ws.send(JSON.stringify(message));
  });
}

async function evaluate(ws, pending, sessionId, expression) {
  const result = await send(ws, pending, 'Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true,
  }, sessionId);
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.text || JSON.stringify(result.exceptionDetails));
  }
  return result.result?.value;
}

function markConsumed() {
  const script = `
import os, sqlite3
db = sqlite3.connect(os.environ["DOCKET_DB"])
db.execute("PRAGMA busy_timeout=5000")
now = "2026-10-16T16:00:00+00:00"
receipt = '{"state":"consumed","admission_id":"msg_panelconsumed01","delivery":"queue"}'
cur = db.execute(
    "UPDATE items SET status='delivered', receipt_json=?, delivered_at=?, updated_at=? WHERE id=? AND status='pending'",
    (receipt, now, now, "dkt_a11ce00000000001"),
)
db.commit()
if cur.rowcount != 1:
    raise SystemExit("consumed update changed %s rows" % cur.rowcount)
`;
  return new Promise((resolve, reject) => {
    const child = spawn('python3', ['-c', script], {env: process.env, stdio: ['ignore', 'pipe', 'pipe']});
    let err = '';
    child.stderr.on('data', chunk => { err += chunk; });
    child.on('exit', code => code === 0 ? resolve() : reject(new Error(err.trim() || 'sqlite update failed')));
  });
}

async function main() {
  const staticServer = await serveStatic();
  const staticPort = staticServer.address().port;
  const chrome = spawn(chromePath, [
    '--headless=new',
    '--disable-gpu',
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-extensions',
    '--disable-background-networking',
    '--hide-scrollbars',
    '--force-device-scale-factor=1',
    `--user-data-dir=${userData}`,
    '--window-size=1000,1200',
    `--remote-debugging-port=${debugPort}`,
    '--remote-debugging-address=127.0.0.1',
    'about:blank',
  ], {stdio: ['ignore', 'pipe', 'pipe']});
  let chromeLog = '';
  chrome.stderr.on('data', chunk => { chromeLog += chunk; });
  chrome.stdout.on('data', chunk => { chromeLog += chunk; });
  const exceptions = [];
  try {
    let version = null;
    for (let attempt = 0; attempt < 50; attempt += 1) {
      try {
        version = await fetch(`http://127.0.0.1:${debugPort}/json/version`).then(response => response.json());
        break;
      } catch {
        await new Promise(resolve => setTimeout(resolve, 200));
      }
    }
    if (!version) throw new Error('Chrome did not open a debugging port. ' + chromeLog.slice(-500));
    const ws = new WebSocket(version.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener('open', resolve);
      ws.addEventListener('error', () => reject(new Error('Chrome debugging socket failed')));
    });
    const pending = {next: 1, map: new Map()};
    ws.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      if (message.id && pending.map.has(message.id)) {
        const waiter = pending.map.get(message.id);
        pending.map.delete(message.id);
        clearTimeout(waiter.timer);
        if (message.error) waiter.reject(new Error(JSON.stringify(message.error)));
        else waiter.resolve(message.result);
        return;
      }
      if (message.method === 'Runtime.exceptionThrown') exceptions.push(message.params?.exceptionDetails?.text || 'exception');
    });
    const {targetId} = await send(ws, pending, 'Target.createTarget', {url: 'about:blank'});
    const attached = await send(ws, pending, 'Target.attachToTarget', {targetId, flatten: true});
    const sessionId = attached.sessionId;
    await send(ws, pending, 'Page.enable', {}, sessionId);
    await send(ws, pending, 'Runtime.enable', {}, sessionId);
    await send(ws, pending, 'Page.navigate', {url: `http://127.0.0.1:${staticPort}/harness.html`}, sessionId);
    await evaluate(ws, pending, sessionId, `new Promise(resolve => {
      if (document.readyState === 'complete') resolve(true);
      else addEventListener('load', () => resolve(true), {once: true});
    })`);
    await evaluate(ws, pending, sessionId, `window.__boot()`);
    if (feedbackCases) {
      const results = [];
      for (const test of feedbackCases) {
        const result = await evaluate(ws, pending, sessionId, `(async () => {
          const test = ${JSON.stringify(test)};
          const wait = async predicate => {
            const end = Date.now() + 8000;
            while (Date.now() < end) {
              if (predicate()) return;
              await new Promise(resolve => setTimeout(resolve, 25));
            }
            throw new Error(test.name + ': ' + window.__text());
          };
          await wait(() => document.getElementById('panel').contentDocument.querySelector('[data-item-id="' + test.id + '"]'));
          window.__nextReply = test.reply || null;
          const count = window.__calls.filter(call => call.path === '/snapshot').length;
          if (window.__clickRemove(test.id) !== 'clicked') throw new Error('missing remove');
          await wait(() => window.__text().includes(test.detail)
            && window.__calls.filter(call => call.path === '/snapshot').length > count
            && !window.__text().includes('Removing'));
          const doc = document.getElementById('panel').contentDocument;
          const text = window.__text();
          if (!text.includes(test.title)) throw new Error('wrong title: ' + text);
          if (test.title !== 'Removed' && /(^|\\n)Removed(\\n|$)/.test(text)) throw new Error('false success: ' + text);
          await wait(() => Boolean(doc.querySelector('[data-item-id="' + test.id + '"]')) === test.outstanding);
          return {name: test.name, title: test.title, detail: test.detail, refreshed: true, outstanding: test.outstanding};
        })()`);
        results.push(result);
      }
      if (exceptions.length) throw new Error(exceptions.join('; '));
      console.log('PANEL_RESULT ' + JSON.stringify({ok: true, results}));
      ws.close();
      return;
    }
    const painted = await evaluate(ws, pending, sessionId, `new Promise((resolve, reject) => {
      const start = Date.now();
      const timer = setInterval(() => {
        let text = '';
        try { text = window.__text() || ''; } catch (error) { text = ''; }
        if (text.includes('ses_panelpending0001') && text.includes('admitted, not consumed')) {
          clearInterval(timer);
          resolve(text);
        } else if (Date.now() - start > 8000) {
          clearInterval(timer);
          reject(new Error(text || 'panel did not paint'));
        }
      }, 50);
    })`);
    const shot = await send(ws, pending, 'Page.captureScreenshot', {format: 'png'}, sessionId);
    fs.writeFileSync(screenshot, Buffer.from(shot.data, 'base64'));
    await markConsumed();
    const consumedClick = await evaluate(ws, pending, sessionId, `window.__clickRemove('dkt_a11ce00000000001')`);
    const consumedText = await evaluate(ws, pending, sessionId, `new Promise((resolve, reject) => {
      const start = Date.now();
      const timer = setInterval(() => {
        const text = window.__text() || '';
        if (text.includes('already consumed')) { clearInterval(timer); resolve(text); }
        else if (Date.now() - start > 8000) { clearInterval(timer); reject(new Error(text)); }
      }, 50);
    })`);
    const admittedClick = await evaluate(ws, pending, sessionId, `window.__clickRemove('dkt_a11ce00000000002')`);
    const admittedText = await evaluate(ws, pending, sessionId, `new Promise((resolve, reject) => {
      const start = Date.now();
      const timer = setInterval(() => {
        const text = window.__text() || '';
        if (text.includes('Removed.')) { clearInterval(timer); resolve(text); }
        else if (Date.now() - start > 8000) { clearInterval(timer); reject(new Error(text)); }
      }, 50);
    })`);
    const historyClick = await evaluate(ws, pending, sessionId, `window.__openHistory()`);
    const historyText = await evaluate(ws, pending, sessionId, `new Promise((resolve, reject) => {
      const start = Date.now();
      const timer = setInterval(() => {
        const text = window.__text() || '';
        if (text.includes('session archived before the fire') && text.includes('host dropped the admission')) {
          clearInterval(timer);
          resolve(text);
        } else if (Date.now() - start > 4000) {
          clearInterval(timer);
          reject(new Error(text));
        }
      }, 50);
    })`);
    const leaked = await evaluate(ws, pending, sessionId, `({
      html: window.__html(),
      text: window.__text(),
      calls: window.__calls,
    })`);
    const html = leaked.html || '';
    const problems = [];
    if (docketToken && (html.includes(docketToken) || (leaked.text || '').includes(docketToken))) problems.push('docket token reached the iframe');
    if (html.includes(serviceToken)) problems.push('relay token reached the iframe');
    if ((leaked.text || '').includes('context-must-not-reach-iframe')) problems.push('context reached the iframe');
    const shown = painted || '';
    for (const expected of [
      '<img src=x onerror=alert(1)>',
      'Set by chief',
      'Set by lead-downloads',
      'ses_paneladmitted001',
      'urgent',
      'late by 42s',
      'Next check',
      'file_stable',
      'Project /example/projects/downloads',
    ]) {
      if (!shown.includes(expected)) problems.push('panel did not show ' + expected);
    }
    if ((leaked.text || '').includes('Not removed') && consumedClick !== 'clicked') problems.push('consumed remove was not clicked');
    const paths = (leaked.calls || []).map(call => call.path);
    if (!paths.every(requestPath => requestPath === '/snapshot' || /^\/items\/dkt_[0-9a-f]{16}\/remove$/.test(requestPath))) {
      problems.push('panel requested a non-relay path: ' + paths.join(','));
    }
    if (exceptions.length) problems.push('page exception: ' + exceptions.join('; '));
    const payload = {
      ok: problems.length === 0 && consumedClick === 'clicked' && admittedClick === 'clicked' && historyClick === 'clicked',
      problems,
      consumedClick,
      admittedClick,
      historyClick,
      paths,
      consumedSeen: (consumedText || '').includes('already consumed'),
      admittedSeen: (admittedText || '').includes('Removed.'),
      historySeen: (historyText || '').includes('session archived before the fire'),
      screenshot,
    };
    console.log('PANEL_RESULT ' + JSON.stringify(payload));
    ws.close();
    if (!payload.ok) process.exitCode = 1;
  } finally {
    chrome.kill('SIGTERM');
    staticServer.close();
  }
}

main().catch(error => {
  console.log('PANEL_RESULT ' + JSON.stringify({ok: false, error: String(error && error.stack || error)}));
  process.exit(1);
});
