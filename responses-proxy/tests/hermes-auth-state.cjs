const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const babel = require('@babel/core');

const filename = path.resolve(__dirname, '../src/hermesAuthState.js');
const source = fs.readFileSync(filename, 'utf8');
const compiled = babel.transformSync(source, { filename, presets: [require.resolve('@babel/preset-env')] }).code;
const loaded = new Module(filename, module);
loaded.filename = filename;
loaded.paths = Module._nodeModulePaths(path.dirname(filename));
loaded._compile(compiled, filename);
const { presentHermesAuth } = loaded.exports;

const base = { connected: false, ready: false, login_required: false,
    session_expired: false, retry_at: null, web_login_enabled: true };
const state = overrides => presentHermesAuth({ ...base, ...overrides }, 2000);
assert.equal(presentHermesAuth(null, 2000).kind, 'initializing');
assert.equal(presentHermesAuth(undefined, 2000).kind, 'unavailable');
assert.equal(state({ connected: true, ready: false }).kind, 'gateway_waiting');
assert.equal(state({ connected: true, ready: true }).kind, 'ready');
assert.equal(state({ web_login_enabled: false }).kind, 'initializing');
assert.equal(state({ login_required: true }).action, 'connect');
assert.equal(state({ login_required: true, session_expired: true }).action, 'reconnect');
assert.equal(state({ login_required: true, web_login_enabled: false }).action, null);
assert.equal(state({ retry_at: 3000 }).kind, 'retrying');
assert.equal(state({ retry_at: 1000 }).kind, 'retrying');
assert.equal(state({ retry_at: null, connected: false }).kind, 'initializing');
assert.equal(state({ login_required: true, session_expired: true, web_login_enabled: false }).action, null);
for (const invalid of [{}, { ...base, connected: 'true' }, { ...base, retry_at: 'later' },
    { ...base, ready: true, connected: false }, { ...base, retry_at: Infinity },
    { ...base, retry_at: 8640000000001 }]) {
    assert.equal(presentHermesAuth(invalid, 2000).kind, 'unavailable');
}
console.log('PASS: Hermes authentication state presentation');
