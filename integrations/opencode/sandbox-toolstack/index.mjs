import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';

export default { id: 'docket.installed-toolstack', async setup(ctx) {
  const readDefinitions = async () => {
    const raw = await readFile(ctx.options.descriptorFile);
    if (createHash('sha256').update(raw).digest('hex') !== ctx.options.descriptorSHA256)
      throw Error('Installed operation descriptors changed; redeploy required');
    return JSON.parse(raw.toString('utf8'));
  };
  const definitions = await readDefinitions();
  await ctx.tool.transform(editor => {
    for (const definition of definitions) {
      editor.add({ name: definition.name, description: definition.description,
        input: { ...definition.schema, properties: { ...definition.schema.properties,
          _reason: { type: 'string', maxLength: 1000, description: 'Bounded justification for the broker review card.' } } },
        options: { codemode: false },
        async execute(input, context) {
          await readDefinitions();
          await ctx.permission.assert({ sessionID: context.sessionID, agent: context.agent,
            source: { type: 'tool', messageID: context.messageID, id: context.id },
            action: definition.name, resources: ['*'] });
          const grant = JSON.parse(await readFile(ctx.options.grantFile, 'utf8'));
          if (grant.binding.session_id !== context.sessionID) throw Error('Native broker session binding denied');
          const key = `broker-call/${grant.binding.admission_id}/${context.id}`;
          const digest = createHash('sha256').update(JSON.stringify(input)).digest('hex');
          const outstandingKey = `broker-outstanding/${grant.binding.admission_id}`;
          const old = await ctx.storage.get(key), outstanding = await ctx.storage.get(outstandingKey);
          if (old && old.digest !== digest) throw Error('Broker call ID argument conflict');
          if (old?.phase === 'recorded') return { content: old.result };
          if (old || outstanding?.phase === 'sending' || outstanding?.phase === 'uncertain')
            throw Error('Uncertain broker effect retained; reconcile its original call ID, never resubmit a new action');
          const { _reason: reason, ...arguments_ } = input;
          await ctx.storage.set(key, { phase: 'sending', digest });
          await ctx.storage.set(outstandingKey, { phase: 'sending', call_id: context.id });
          try {
            const response = await fetch(ctx.options.gatewayURL + `/v1/actions/${definition.tool}.${definition.op}`, {
              method: 'POST', signal: context.signal,
              headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + grant.token,
                'X-Docket-Binding': JSON.stringify(grant.binding), 'X-Docket-Call-ID': context.id,
                'X-Docket-Operation': `${definition.tool}.${definition.op}` },
              body: JSON.stringify({ arguments: arguments_, ...(reason ? { reason } : {}) }),
            });
            const result = JSON.stringify({ http_status: response.status, ...await response.json() });
            await ctx.storage.set(key, { phase: 'recorded', digest, result });
            await ctx.storage.set(outstandingKey, { phase: 'recorded', call_id: context.id });
            return { content: result };
          } catch (error) {
            await ctx.storage.set(outstandingKey, { phase: 'uncertain', call_id: context.id });
            throw Error('Broker transport/effect uncertain; original call ID retained, no new action retry');
          }
        },
      });
    }
  });
} };
