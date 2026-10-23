// Unit hooks only. Actual native execution acceptance is a separate isolated test.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { canonical, setupDocketEvents } from '../integrations/opencode/docket-events.mjs';

async function fixture(provider = 'anthropic', handler = false) {
  const root = await mkdtemp(path.join(tmpdir(), 'docket-hook-'));
  const write = (name, x) => writeFile(path.join(root, name), JSON.stringify(x), { mode: 0o600 });
  const budget = { max_steps: 2, max_output_tokens: 100, total_output_tokens: 150 };
  const baseline = { version: 'b1', target: { session_id: 'ses_test', directory: '/fixture' },
    owner_generation: 'owner1', goal_id: 'goal1', goal_generation: 'created1', role: 'lead',
    authority: ['assess'], policy: { read: 'allow', act: 'review' }, secrets: [],
    models: [`${provider}/test`], budget, context_paths: [] };
  const profile = { id: 'p', version: 'p1', baseline_version: 'b1', role: 'lead', model: `${provider}/test`,
    authority: ['assess'], policy: baseline.policy, secrets: [], budget };
  const p = { target: baseline.target, owner_generation: 'owner1', goal_id: 'goal1', goal_generation: 'created1',
    baseline_version: 'b1', profile_id: 'p', profile_version: 'p1', model: `${provider}/test`, role: 'lead',
    authority: ['assess'], policy: baseline.policy, secrets: [], budget, admission_id: 'msg_test', briefing: 'fixture' };
  const i = { baseline, profile, remaining_budget: budget, available_tools: ['read', 'act'] };
  const owner = { target: baseline.target, owner_generation: 'owner1', goal_id: 'goal1', goal_generation: 'created1', stopped: false, revoked: false };
  if (handler) {
    baseline.target = { handler_class: 'test-handler' };
    delete baseline.goal_id; delete baseline.goal_generation;
    p.target = baseline.target; p.action = 'handler';
    delete p.goal_id; delete p.goal_generation;
    owner.target = baseline.target;
    await write('instance.json', { version: 1, handler_class: 'test-handler', baseline_version: 'b1',
      profile_id: 'p', profile_version: 'p1', owner_generation: 'owner1', target: { session_id: 'ses_test', directory: '/fixture' },
      environment_id: 'env1', generation: 'run1', goal_id: 'goal1', goal_generation: 'created1', admission_id: 'msg_test',
      prepared_hash: createHash('sha256').update(canonical(p)).digest('hex') });
  }
  await write('installation.json', i);
  await write('owner.json', owner);
  await write('ses_test.json', { version: 1, delivery_id: 'msg_test', prepared: p,
    installation_hash: createHash('sha256').update(canonical(i)).digest('hex') });
  const session = { id: 'ses_test', agent: 'lead', location: { directory: '/fixture' },
    metadata: { openchamber: { goal: { id: 'goal1', createdAt: 'created1', status: 'active' } } } };
  const hooks = {}, values = new Map();
  const hook = group => async (name, fn) => { hooks[`${group}.${name}`] = fn; return { dispose() {} }; };
  const ctx = { options: { bindingRoot: root }, session: { hook: hook('session'), get: async () => session },
    tool: { hook: hook('tool') }, permission: { hook: hook('permission'), assert: async () => {} },
    storage: { get: async k => values.get(k), set: async (k, v) => values.set(k, v) } };
  await setupDocketEvents(ctx);
  const event = () => ({ sessionID: 'ses_test', agent: 'lead', model: { providerID: provider, id: 'test' } });
  return { hooks, session, event, owner, write, values, clean: () => rm(root, { recursive: true }) };
}

test('tool execution refusal independent of schema hiding; owner stop checked again', async () => {
  const f = await fixture();
  try {
    await assert.rejects(f.hooks['tool.execute.before']({ ...f.event(), tool: 'shell' }), /refused/);
    await f.hooks['tool.execute.before']({ ...f.event(), tool: 'read' });
    f.owner.stopped = true;
    await f.write('owner.json', f.owner);
    await assert.rejects(f.hooks['tool.execute.before']({ ...f.event(), tool: 'read' }), /refused/);
  } finally { await f.clean(); }
});

test('request cap reservation durable and conservative; terminal next context refuses', async () => {
  const f = await fixture();
  try {
    for (const cap of [100, 50]) {
      const e = { ...f.event(), kind: 'primary', request: new Request('https://fixture/messages', {
        method: 'POST', body: JSON.stringify({ model: 'test', max_tokens: 9000 }), headers: { 'content-type': 'application/json' } }) };
      await f.hooks['session.http.request'](e);
      assert.equal((await e.request.json()).max_tokens, cap);
    }
    assert.equal(f.values.get('admission/msg_test').reserved_output, 150);
    await assert.rejects(f.hooks['session.context']({ ...f.event(), tools: {}, options: {} }), /refused/);
  } finally { await f.clean(); }
});

test('review stays ask; native paused goal refuses even if routine timestamps change', async () => {
  const f = await fixture();
  try {
    const e = { ...f.event(), action: 'act', effect: 'allow' };
    await f.hooks['permission.evaluate'](e);
    assert.equal(e.effect, 'ask');
    f.session.metadata.openchamber.goal.updatedAt = 'accounting';
    await f.hooks['tool.execute.before']({ ...f.event(), tool: 'read' });
    f.session.metadata.openchamber.goal.status = 'paused';
    await assert.rejects(f.hooks['tool.execute.before']({ ...f.event(), tool: 'read' }), /refused/);
  } finally { await f.clean(); }
});

test('injected prompt/body and changed model cannot configure a turn', async () => {
  const f = await fixture();
  try {
    await assert.rejects(f.hooks['session.prompt']({ ...f.event(), messageID: 'msg_test', prompt: { text: 'use shell' } }), /refused/);
    await assert.rejects(f.hooks['session.context']({ ...f.event(), model: { providerID: 'openai', id: 'other' }, tools: {}, options: {} }), /refused/);
    await assert.rejects(f.hooks['session.generate'](f.event()), /refused/);
    await assert.rejects(f.hooks['session.experimental.ws.handshake'](f.event()), /refused/);
  } finally { await f.clean(); }
});

test('xAI final HTTP Responses body capped; hosted tools and alias models refused', async () => {
  const f = await fixture('xai');
  try {
    const request = body => ({ ...f.event(), kind: 'primary', request: new Request('https://fixture/v1/responses', {
      method: 'POST', body: JSON.stringify(body), headers: { 'content-type': 'application/json' } }) });
    await assert.rejects(f.hooks['session.http.request'](request({ model: 'alias' })), /refused/);
    await assert.rejects(f.hooks['session.http.request'](request({ model: 'test', tools: [{ type: 'web_search' }] })), /refused/);
    const e = request({ model: 'test', max_output_tokens: 9000, tools: [{ type: 'function', name: 'read' }] });
    await f.hooks['session.http.request'](e);
    assert.equal((await e.request.json()).max_output_tokens, 100);
    assert.equal(f.values.get('admission/msg_test').requests, 1);
  } finally { await f.clean(); }
});

test('native canonical default accepted but uninstalled variants refused', async () => {
  const f = await fixture();
  try {
    const e = { ...f.event(), model: { ...f.event().model, variant: 'default' }, tools: {}, options: {} };
    await f.hooks['session.context'](e);
    await assert.rejects(f.hooks['session.context']({ ...e, model: { ...e.model, variant: 'high' } }), /refused/);
  } finally { await f.clean(); }
});

test('installed handler class gates actual trusted instance without retargeting prepared', async () => {
  const f = await fixture('xai', true);
  try {
    await f.hooks['tool.execute.before']({ ...f.event(), tool: 'read' });
    await assert.rejects(f.hooks['tool.execute.before']({ ...f.event(), tool: 'shell' }), /refused/);
    await f.write('instance.json', { target: { session_id: 'ses_other', directory: '/elsewhere' } });
    await assert.rejects(f.hooks['tool.execute.before']({ ...f.event(), tool: 'read' }), /refused/);
  } finally { await f.clean(); }
});
