const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const babel = require('@babel/core');

const filename = path.resolve(__dirname, '../src/serverRequests.js');
const source = fs.readFileSync(filename, 'utf8');
const transformed = babel.transformSync(source, {
    filename,
    presets: [['@babel/preset-env', { modules: 'commonjs' }]],
}).code;
const sandboxModule = { exports: {} };
vm.runInNewContext(transformed, { module: sandboxModule, exports: sandboxModule.exports }, { filename });
const {
    normalizeServerRequest,
    normalizeServerRequestSnapshot,
    removeServerRequest,
    buildClarifyLock,
    buildServerRequestResponse,
    classifyServerRequestError,
    upsertServerRequest,
} = sandboxModule.exports;

const approval = (rpc_id, command = 'echo redacted') => ({
    type: 'server_request',
    session_id: 'chat-1',
    rpc_id,
    method: 'approval',
    params: {
        request_id: `approval-${rpc_id}`,
        command,
        description: 'Approval required',
        choices: ['once', 'session', 'always', 'deny'],
    },
});

// Native ids, not internal approval request ids, identify UI cards.
const first = normalizeServerRequest(approval('srq-aaa111'));
const second = normalizeServerRequest(approval('srq-bbb222', 'echo redacted'));
assert.strictEqual(first.rpc_id, 'srq-aaa111');
assert.strictEqual(first.params.request_id, 'approval-srq-aaa111');
assert.notStrictEqual(first.rpc_id, first.params.request_id);
assert.deepStrictEqual(
    JSON.parse(JSON.stringify(buildServerRequestResponse('chat-1', first, { choice: 'once' }))),
    {
        type: 'server_request_response',
        session_id: 'chat-1',
        rpc_id: 'srq-aaa111',
        method: 'approval',
        result: { choice: 'once' },
    },
);

let requests = upsertServerRequest([], first);
requests = upsertServerRequest(requests, second);
assert.strictEqual(requests.map((item) => item.rpc_id).join(','), 'srq-aaa111,srq-bbb222');
assert.strictEqual(upsertServerRequest(requests, first).length, 2);
assert.strictEqual(removeServerRequest(requests, { session_id: 'chat-1', rpc_id: 'srq-aaa111', method: 'approval' }).length, 1);
assert.strictEqual(requests.length, 2, 'reducers do not mutate the previous snapshot');

const batch = normalizeServerRequest({
    type: 'server_request',
    session_id: 'chat-1',
    rpc_id: 'srq-ccc333',
    method: 'clarify',
    params: {
        questions: [
            { qid: 'q1', question: 'First?', choices: ['yes'], multi_select: false },
            { qid: 'q2', question: 'Second?', choices: [], multi_select: false },
        ],
    },
});
assert.strictEqual(batch.params.questions.length, 2);
assert.deepStrictEqual(
    JSON.parse(JSON.stringify(buildClarifyLock('chat-1', batch, 'q1', 'yes'))),
    {
        type: 'server_request_clarify_lock',
        session_id: 'chat-1',
        rpc_id: 'srq-ccc333',
        question_id: 'q1',
        answer: 'yes',
    },
);
assert.strictEqual(normalizeServerRequest({ ...approval('srq-bad'), method: 'secret' }), null);
assert.strictEqual(classifyServerRequestError({
    type: 'server_request_error', session_id: 'chat-1', rpc_id: 'srq-aaa111', method: 'approval', status: 'rejected',
}), 'retry');
assert.strictEqual(classifyServerRequestError({
    type: 'server_request_error', session_id: 'chat-1', rpc_id: 'srq-bbb222', method: 'approval', status: 'delivery_uncertain',
}), 'remove');
assert.strictEqual(classifyServerRequestError({
    type: 'server_request_error', session_id: 'chat-1', rpc_id: 'srq-bbb222', method: 'approval', status: 'rejected', secret: 'nope',
}), 'retry');
assert.strictEqual(
    normalizeServerRequestSnapshot([first, first, batch]).map((item) => item.rpc_id).join(','),
    'srq-aaa111,srq-ccc333',
);

console.log('PASS: native server-request reducer');
