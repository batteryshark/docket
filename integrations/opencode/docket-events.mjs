// Native V2 plugin. Install LAST, in the isolated host only. No tools registered.
// Trusted root must be denied to all model tools AND their execution environment.
import { open, lstat } from 'node:fs/promises';
import { constants } from 'node:fs';
import { createHash } from 'node:crypto';
import path from 'node:path';

export function canonical(x) {
  if (Array.isArray(x)) return `[${x.map(canonical).join(',')}]`;
  if (x && typeof x === 'object') return `{${Object.keys(x).sort().map(k => `${JSON.stringify(k)}:${canonical(x[k])}`).join(',')}}`;
  // Python canonical uses ensure_ascii=True; preserve a shared cross-language hash.
  return JSON.stringify(x)?.replace(/[\u007f-\uffff]/g, c => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`);
}
const hash = x => createHash('sha256').update(canonical(x)).digest('hex');
const refuse = () => { throw Error('Docket native gate refused: binding, generation, policy or budget'); };

export async function privateJSON(file) {
  const f = await open(file, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const s = await f.stat();
    if (!s.isFile() || s.uid !== process.getuid() || (s.mode & 0o077) || s.nlink !== 1 || s.size > 131072) refuse();
    const raw = await f.readFile('utf8');
    if (Buffer.byteLength(raw) > 131072) refuse();
    return JSON.parse(raw);
  } finally { await f.close(); }
}

export async function setupDocketEvents(ctx) {
  const root = ctx.options.bindingRoot;
  if (typeof root !== 'string' || !path.isAbsolute(root)) refuse();
  const s = await lstat(root);
  if (!s.isDirectory() || s.isSymbolicLink() || s.uid !== process.getuid() || (s.mode & 0o077)) refuse();
  const installation = () => privateJSON(path.join(root, 'installation.json'));
  const initial = await installation();
  const installedHash = hash(initial);
  const handler = Object.hasOwn(initial.baseline.target, 'handler_class');
  const instance = handler ? await privateJSON(path.join(root, 'instance.json')) : null;
  const instanceHash = handler ? hash(instance) : null;
  const protectedID = handler ? instance.target.session_id : initial.baseline.target.session_id;
  if (!/^ses_[A-Za-z0-9]+$/.test(protectedID)) refuse();
  let serial = Promise.resolve();
  const locked = fn => {
    const result = serial.then(fn);
    serial = result.catch(() => {});
    return result;
  };
  async function gate(event) {
    // A changed target must not turn the newly selected owner into an unguarded
    // session. Installation upgrades require explicit plugin reload/rebinding.
    if (hash(await installation()) !== installedHash) refuse();
    if (handler && hash(await privateJSON(path.join(root, 'instance.json'))) !== instanceHash) refuse();
    if (event.sessionID !== protectedID) return null;
    const [i, owner, binding, session] = await Promise.all([
      installation(), privateJSON(path.join(root, 'owner.json')),
      privateJSON(path.join(root, protectedID + '.json')),
      ctx.session.get({ sessionID: protectedID }),
    ]);
    const p = binding.prepared, b = i.baseline, profile = i.profile;
    if (binding.version !== 1 || binding.installation_hash !== hash(i)
        || owner.stopped !== false || owner.revoked !== false) refuse();
    for (const k of handler ? ['target', 'owner_generation'] : ['target', 'owner_generation', 'goal_id', 'goal_generation']) {
      if (canonical(p[k]) !== canonical(owner[k]) || canonical(p[k]) !== canonical(b[k])) refuse();
    }
    const nativeTarget = handler ? instance.target : p.target;
    const goalID = handler ? instance.goal_id : p.goal_id;
    const goalGeneration = handler ? instance.goal_generation : p.goal_generation;
    if (handler && (p.action !== 'handler' || instance.version !== 1
        || instance.handler_class !== p.target.handler_class || instance.prepared_hash !== hash(p)
        || instance.admission_id !== p.admission_id || instance.baseline_version !== p.baseline_version
        || instance.profile_id !== p.profile_id || instance.profile_version !== p.profile_version
        || instance.owner_generation !== p.owner_generation || !instance.generation || !instance.environment_id)) refuse();
    if (p.baseline_version !== b.version || profile.baseline_version !== b.version
        || p.profile_id !== profile.id || p.profile_version !== profile.version
        || p.role !== b.role || profile.role !== b.role || session.agent !== b.role
        || (event.agent !== undefined && event.agent !== b.role)
        || session.id !== protectedID || session.location?.directory !== nativeTarget.directory
        || session.time?.archived || profile.model !== p.model || !b.models.includes(p.model)) refuse();
    const goal = session.metadata?.openchamber?.goal;
    if (goal?.id !== goalID || goal?.createdAt !== goalGeneration || goal?.status !== 'active') refuse();
    if (event.model && `${event.model.providerID}/${event.model.id}` !== p.model) refuse();
    if (event.model?.variant && event.model.variant !== 'default') refuse(); // Native canonical default is not a new variant.
    const levels = { deny: 0, review: 1, allow: 2 };
    for (const [tool, effect] of Object.entries(p.policy)) {
      if (!Object.hasOwn(levels, effect) || effect === 'deny'
          || profile.policy[tool] !== effect || !Object.hasOwn(b.policy, tool)
          || levels[effect] > levels[b.policy[tool]]) refuse();
    }
    for (const [tool, effect] of Object.entries(profile.policy)) {
      if (effect !== 'deny' && p.policy[tool] !== effect) refuse();
    }
    for (const key of ['authority', 'secrets']) {
      if (canonical(p[key]) !== canonical(profile[key]) || p[key].some(x => !b[key].includes(x))) refuse();
    }
    for (const k of ['max_steps', 'max_output_tokens', 'total_output_tokens']) {
      if (!Number.isSafeInteger(p.budget[k]) || p.budget[k] < 1
          || p.budget[k] !== Math.min(profile.budget[k], i.remaining_budget[k])
          || profile.budget[k] > b.budget[k]) refuse();
    }
    return { p, key: `admission/${p.admission_id}`, fingerprint: hash(binding) };
  }
  async function state(binding) {
    const old = await ctx.storage.get(binding.key);
    if (old && old.fingerprint !== binding.fingerprint) refuse();
    return old ?? { fingerprint: binding.fingerprint, requests: 0, reserved_output: 0 };
  }
  const registrations = [];
  registrations.push(await ctx.session.hook('prompt', async e => {
    const binding = await gate(e);
    if (!binding) return;
    if (e.messageID !== binding.p.admission_id || e.prompt.text !== binding.p.briefing
        || e.prompt.files?.length || e.prompt.agents?.length || e.prompt.skills?.length) refuse();
    e.delivery = 'queue';
  }));
  registrations.push(await ctx.session.hook('context', async e => {
    const binding = await gate(e);
    if (!binding) return;
    const { p } = binding, counters = await state(binding);
    if (counters.requests >= p.budget.max_steps || counters.reserved_output >= p.budget.total_output_tokens) refuse();
    const i = await installation();
    if ((i.profile.required_tools ?? []).some(name => !Object.hasOwn(e.tools, name))) refuse();
    // Schemas alone are not the boundary: execute.before independently rechecks.
    for (const name of Object.keys(e.tools)) if (!p.policy[name]) delete e.tools[name];
    e.options.maxTokens = Math.min(p.budget.max_output_tokens, p.budget.total_output_tokens - counters.reserved_output);
  }));
  for (const name of ['compaction', 'generate']) registrations.push(await ctx.session.hook(name, async e => {
    if (await gate(e)) refuse(); // No unbudgeted auxiliary requests.
  }));
  registrations.push(await ctx.session.hook('title', e => {
    if (e.sessionID === protectedID) e.result = 'Docket isolated owner';
  }));
  registrations.push(await ctx.session.hook('model.request', async e => { await gate(e); }));
  registrations.push(await ctx.session.hook('experimental.ws.handshake', async e => {
    if (await gate(e)) refuse(); // Only audited capped HTTP route supported.
  }));
  registrations.push(await ctx.session.hook('retry', async e => {
    if (e.sessionID === protectedID) e.decision = { retry: false };
  }));
  registrations.push(await ctx.session.hook('http.request', e => locked(async () => {
    const binding = await gate(e);
    if (!binding) return;
    // OpenAI subscription intentionally omits caps: REFUSE, never call it capped.
    if (e.kind !== 'primary' || !['anthropic', 'xai'].includes(e.model.providerID)) refuse();
    const counters = await state(binding), { p } = binding;
    const cap = Math.min(p.budget.max_output_tokens, p.budget.total_output_tokens - counters.reserved_output);
    if (counters.requests >= p.budget.max_steps || cap < 1) refuse();
    const body = await e.request.clone().json();
    if (body.model !== e.model.id) refuse(); // No uninstalled wire-model aliases.
    // Also refuse provider-hosted tools: those bypass native execute.before.
    for (const tool of body.tools ?? []) {
      if (e.model.providerID === 'xai' && tool.type !== 'function') refuse();
      if (!p.policy[tool.name]) refuse();
    }
    if (e.model.providerID === 'anthropic') {
      body.max_tokens = cap;
      if (body.thinking?.budget_tokens >= cap) refuse();
    } else {
      if (!new URL(e.request.url).pathname.endsWith('/responses')) refuse();
      body.max_output_tokens = cap;
    }
    const headers = new Headers(e.request.headers);
    headers.delete('content-length');
    e.request = new Request(e.request, { headers, body: JSON.stringify(body) });
    await ctx.storage.set(binding.key, { ...counters, requests: counters.requests + 1,
      reserved_output: counters.reserved_output + cap,
      last_cap: cap, provider: e.model.providerID }); // Before sending; never refund ambiguity.
  })));
  registrations.push(await ctx.tool.hook('execute.before', async e => {
    const binding = await gate(e);
    if (!binding) return;
    const effect = binding.p.policy[e.tool];
    if (!effect || effect === 'deny') refuse();
    if (effect === 'review') await ctx.permission.assert({ sessionID: e.sessionID, agent: e.agent,
      source: { type: 'tool', messageID: e.messageID, id: e.id }, action: e.tool, resources: ['*'] });
    // Recheck after a possibly long human approval wait.
    await gate(e);
  }));
  registrations.push(await ctx.permission.hook('evaluate', async e => {
    const binding = await gate(e);
    if (!binding) return;
    const effect = binding.p.policy[e.action];
    if (!effect || effect === 'deny') e.effect = 'deny';
    else if (effect === 'review' && e.effect === 'allow') e.effect = 'ask';
  }));
  return async () => { for (const r of registrations) await r.dispose(); };
}

export default { id: 'docket-events', setup: setupDocketEvents };
