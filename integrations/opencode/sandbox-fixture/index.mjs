import { readFile } from 'node:fs/promises';

export default { id: 'docket.sandbox-fixture', async setup(ctx) {
  await ctx.tool.transform(editor => {
    editor.add({
      name: 'docket_fixture',
      description: 'Write the owned test fixture, request policy review, or verify a forbidden operation is denied. Never accepts paths or URLs.',
      input: { type: 'object', properties: { operation: { type: 'string', enum: ['write', 'review', 'forbidden', 'refresh', 'refresh_one'] } }, required: ['operation'], additionalProperties: false },
      options: { codemode: false },
      async execute(input, context) {
        await ctx.permission.assert({ sessionID: context.sessionID, agent: context.agent,
          source: { type: 'tool', messageID: context.messageID, id: context.id },
          action: 'docket_fixture', resources: ['*'] });
        const grant = JSON.parse(await readFile(ctx.options.grantFile, 'utf8'));
        if (grant.binding.session_id !== context.sessionID) throw Error('Fixture session binding denied');
        const response = await fetch((ctx.options.gatewayURL ?? 'http://127.0.0.1:47962') + '/v1/actions/fixture.' + input.operation, {
          method: 'POST', signal: context.signal,
          headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + grant.token,
            'X-Docket-Binding': JSON.stringify(grant.binding), 'X-Docket-Call-ID': context.id },
          body: JSON.stringify({ arguments: {} }),
        });
        const result = await response.json();
        return { content: JSON.stringify({ http_status: response.status, ...result }) };
      },
    });
  });
} };
