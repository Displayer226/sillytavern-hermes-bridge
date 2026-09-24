const assert = require('assert');
const fs = require('fs');
const path = require('path');
const util = require('util');
const vm = require('vm');
const babel = require('@babel/core');

const root = path.resolve(__dirname, '..');
const moduleCache = new Map();

const instances = [];

// Record every console message emitted inside the module sandboxes so the
// token-leakage regression sweep can inspect all of them.
const recordedLogs = [];
const recordingConsole = {};
for (const level of ['log', 'info', 'warn', 'error']) {
    recordingConsole[level] = (...args) => {
        recordedLogs.push(util.format(...args));
    };
}

class FakeWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;
    // Regression hook: browsers throw from the constructor when the URL is
    // rejected, and the error message embeds the full URL.
    static shouldThrowOnConstruct = false;

    constructor(url, protocols) {
        if (FakeWebSocket.shouldThrowOnConstruct) {
            throw new Error(`Failed to construct 'WebSocket': the URL '${url}' is invalid`);
        }
        this.url = url;
        // Browsers accept a string or an array of subprotocols; normalize so
        // assertions can inspect exactly what the handshake requested.
        this.protocols = Array.isArray(protocols)
            ? protocols.slice()
            : (protocols ? [protocols] : []);
        this.readyState = FakeWebSocket.CONNECTING;
        this.sent = [];
        instances.push(this);
    }

    send(payload) {
        this.sent.push(JSON.parse(payload));
    }

    close() {
        this.readyState = FakeWebSocket.CLOSED;
        if (this.onclose) this.onclose({ code: 1000, reason: 'closed' });
    }

    open() {
        this.readyState = FakeWebSocket.OPEN;
        if (this.onopen) this.onopen();
    }
}

global.WebSocket = FakeWebSocket;
global.window = {
    location: { protocol: 'http:', host: 'localhost:8000' },
};

function loadModule(request, fromDir = path.join(root, 'src')) {
    const filename = path.resolve(fromDir, `${request}.js`);
    if (moduleCache.has(filename)) return moduleCache.get(filename).exports;

    const source = fs.readFileSync(filename, 'utf8');
    const transformed = babel.transformSync(source, {
        filename,
        presets: [['@babel/preset-env', { modules: 'commonjs' }]],
    }).code;

    const module = { exports: {} };
    moduleCache.set(filename, module);
    const localRequire = (specifier) => {
        if (specifier.startsWith('.')) {
            return loadModule(specifier, path.dirname(filename));
        }
        return require(specifier);
    };
    vm.runInNewContext(transformed, {
        console: recordingConsole,
        module,
        exports: module.exports,
        require: localRequire,
        WebSocket: FakeWebSocket,
        window: global.window,
        setTimeout,
        clearTimeout,
        URL,
        Headers,
        btoa,
        atob,
        TextEncoder,
        TextDecoder,
    }, { filename });
    return module.exports;
}

const wsModule = loadModule('./WebSocketService');
const httpModule = loadModule('./ProxyHttp');
const wsService = wsModule.default;

const FAKE_SECRET = 'SUPERFAKESECRETCODE123';
const APP_PROTOCOL = wsModule.WS_APPLICATION_PROTOCOL;

assert.strictEqual(APP_PROTOCOL, 'sillytavern-hermes-bridge');

// base64url helpers mirrored from the contract so the synthetic auth
// subprotocol can be decoded back to the token.
function decodeBase64UrlUtf8(encoded) {
    assert.ok(!encoded.includes('='), 'base64url must be unpadded');
    const b64 = encoded.replace(/-/g, '+').replace(/_/g, '/');
    const binary = atob(b64);
    const bytes = Uint8Array.from(binary, (ch) => ch.charCodeAt(0));
    return new TextDecoder('utf-8').decode(bytes);
}

function makeContext(token, wsUrl) {
    return {
        extensionSettings: {
            responsesProxy: {
                ...(token !== undefined ? { wsToken: token } : {}),
                ...(wsUrl !== undefined ? { wsUrl } : {}),
            },
        },
    };
}

// ─── 1. URL derivation carries no token query ──────────────────────────

// Manual override: token configured, URL stays exactly as configured.
assert.strictEqual(
    wsModule.deriveWsUrl(makeContext(FAKE_SECRET, 'ws://localhost:8010/ws')),
    'ws://localhost:8010/ws',
);
// Manual override with a pre-existing (non-auth) query is preserved verbatim.
assert.strictEqual(
    wsModule.deriveWsUrl(makeContext(FAKE_SECRET, 'ws://localhost:8010/ws?room=1')),
    'ws://localhost:8010/ws?room=1',
);

// Custom endpoint reachable from the browser: same route, no auth query.
assert.strictEqual(
    wsModule.deriveWsUrl({
        chatCompletionSettings: {
            chat_completion_source: 'custom',
            custom_url: 'http://proxy.example.test:8010/v1',
        },
        extensionSettings: { responsesProxy: { wsToken: FAKE_SECRET } },
    }),
    'ws://proxy.example.test:8010/ws',
);

// Docker-internal hostname falls back to the reverse proxy route, token-free.
assert.strictEqual(
    wsModule.deriveWsUrl({
        chatCompletionSettings: {
            chat_completion_source: 'custom',
            custom_url: 'http://sillytavern-session-proxy:8010/v1',
        },
        extensionSettings: { responsesProxy: { wsToken: FAKE_SECRET } },
    }),
    'ws://localhost:8000/proxy-ws/ws',
);

// No token configured: fallback is identical.
assert.strictEqual(
    wsModule.deriveWsUrl(makeContext(undefined)),
    'ws://localhost:8000/proxy-ws/ws',
);

// ─── 2. HTTP helpers keep working (unchanged surface) ──────────────────

assert.strictEqual(httpModule.proxyHttpBaseUrl(makeContext(FAKE_SECRET, 'ws://localhost:8010/ws')), 'http://localhost:8010');
assert.strictEqual(httpModule.proxyAuthHeaders(makeContext(FAKE_SECRET)).get('Authorization'), `Bearer ${FAKE_SECRET}`);
// HTTP file URLs still carry the token query by design; only the WebSocket
// URL must be token-free.
assert.strictEqual(
    httpModule.withProxyAuthToken(makeContext(FAKE_SECRET), 'http://localhost:8010/file?path=a'),
    `http://localhost:8010/file?path=a&token=${FAKE_SECRET}`,
);

// ─── 3. Handshake: clean URL + both subprotocols, token never in URL ───

wsService.setContext(makeContext(FAKE_SECRET));
wsService.connect(null, wsModule.deriveWsUrl(makeContext(FAKE_SECRET, 'ws://localhost:8010/ws')));

assert.strictEqual(instances.length, 1);
const socket = instances[0];

// The actual URL handed to WebSocket: same route as before, no auth query,
// no token value anywhere in it.
assert.strictEqual(socket.url, 'ws://localhost:8010/ws');
assert.ok(!socket.url.includes('token='), 'URL must not carry a token query');
assert.ok(!socket.url.includes(FAKE_SECRET), 'URL must not embed the token');
assert.ok(!socket.url.includes('?'), 'URL must have no query string at all');

// Both requested protocols are present, application protocol first.
assert.strictEqual(socket.protocols.length, 2);
assert.strictEqual(socket.protocols[0], APP_PROTOCOL);
assert.ok(socket.protocols[1].startsWith('auth.'), 'second protocol must be the auth subprotocol');

// The synthetic auth protocol decodes back to the fake token.
assert.strictEqual(
    decodeBase64UrlUtf8(socket.protocols[1].slice('auth.'.length)),
    FAKE_SECRET,
);

socket.open();
assert.deepStrictEqual(socket.sent, []);

// ─── 4. Session flows preserved on the clean connection ────────────────

wsService.subscribe('chat-1');
assert.strictEqual(instances.length, 1);
assert.deepStrictEqual(socket.sent[0], { type: 'subscribe', session_id: 'chat-1' });

wsService.subscribe(null);
assert.deepStrictEqual(socket.sent[1], { type: 'unsubscribe', session_id: 'chat-1' });

// Same URL + same token + active socket: reuse, no second socket.
wsService.connect('chat-2', wsModule.deriveWsUrl(makeContext(FAKE_SECRET, 'ws://localhost:8010/ws')));
assert.strictEqual(instances.length, 1);
assert.deepStrictEqual(socket.sent[2], { type: 'subscribe', session_id: 'chat-2' });

// ─── 5. Token change reopens the socket with the new auth protocol ─────

wsService.setContext(makeContext('ROTATED-TOKEN'));
wsService.connect('chat-2', wsModule.deriveWsUrl(makeContext('ROTATED-TOKEN', 'ws://localhost:8010/ws')));
assert.strictEqual(instances.length, 2, 'token change must reopen the socket');
const rotated = instances[1];
assert.strictEqual(rotated.url, 'ws://localhost:8010/ws');
assert.strictEqual(rotated.protocols[0], APP_PROTOCOL);
assert.notStrictEqual(rotated.protocols[1], socket.protocols[1]);
assert.strictEqual(decodeBase64UrlUtf8(rotated.protocols[1].slice('auth.'.length)), 'ROTATED-TOKEN');
rotated.open();

// ─── 6. No token configured: only the application protocol requested ───

wsService.disconnect();
instances.length = 0;
wsService.setContext(makeContext(undefined));
wsService.connect(null, wsModule.deriveWsUrl(makeContext(undefined, 'ws://localhost:8010/ws')));
assert.strictEqual(instances.length, 1);
assert.strictEqual(instances[0].protocols.length, 1);
assert.strictEqual(instances[0].protocols[0], APP_PROTOCOL);
instances[0].open();

// ─── 7. Token-leakage regression sweep ─────────────────────────────────
// A recognizable fake secret must never appear in any logged message, and
// the auth subprotocol itself must never be logged either.

wsService.disconnect();
instances.length = 0;
recordedLogs.length = 0;

// 7a. Constructor-throw path: the fake Error embeds the full URL, exactly
// like a browser TypeError would.
wsService.setContext(makeContext(FAKE_SECRET));
FakeWebSocket.shouldThrowOnConstruct = true;
wsService.connect(null, wsModule.deriveWsUrl(makeContext(FAKE_SECRET, 'ws://localhost:8010/ws')));
FakeWebSocket.shouldThrowOnConstruct = false;
wsService.disconnect();

// 7b. Runtime error event: browsers expose the full URL via event.target.
wsService.connect(null, wsModule.deriveWsUrl(makeContext(FAKE_SECRET, 'ws://localhost:8010/ws')));
const errorSocket = instances[instances.length - 1];
assert.strictEqual(errorSocket.url, 'ws://localhost:8010/ws', 'connection URL stays token-free');
errorSocket.onerror({ target: { url: errorSocket.url } });
wsService.disconnect();

// 7c. URL-derivation fallback log uses a redacted representation.
wsModule.deriveWsUrl({
    chatCompletionSettings: {
        chat_completion_source: 'custom',
        custom_url: 'http://sillytavern-session-proxy:8010/v1',
    },
    extensionSettings: { responsesProxy: { wsToken: FAKE_SECRET } },
});

assert.ok(recordedLogs.length >= 3, 'expected log output from the exercised paths');
for (const message of recordedLogs) {
    assert.ok(!message.includes(FAKE_SECRET), `log leaked the fake secret: ${message}`);
    assert.ok(!/[?&]token=/.test(message), `log contains a token parameter: ${message}`);
    assert.ok(!/auth\.[A-Za-z0-9_-]/.test(message), `log contains an auth subprotocol: ${message}`);
}

// Only the runtime-error path created a socket (the constructor-throw path
// fails before any instance exists).
assert.strictEqual(instances.length, 1);
assert.strictEqual(instances[0].protocols.length, 2);
assert.strictEqual(decodeBase64UrlUtf8(instances[0].protocols[1].slice('auth.'.length)), FAKE_SECRET);

// ─── 8. UTF-8 / non-ASCII token round-trip ─────────────────────────────

const unicodeToken = 'sécurité-鍵-🔐-token';
const encoded = wsModule.encodeBase64UrlUtf8(unicodeToken);
assert.ok(!/[+/=]/.test(encoded), 'encoding must be base64url without padding');
assert.strictEqual(decodeBase64UrlUtf8(encoded), unicodeToken);

// buildAuthProtocol wraps the encoding in the auth. namespace.
assert.strictEqual(
    wsModule.buildAuthProtocol(unicodeToken),
    `auth.${encoded}`,
);

// ─── 9. redactWsUrl still strips any query for logging ─────────────────

assert.strictEqual(
    wsModule.redactWsUrl('wss://host.example/proxy-ws/ws?token=whatever'),
    'wss://host.example/proxy-ws/ws',
);
assert.strictEqual(wsModule.redactWsUrl(null), '<none>');
assert.strictEqual(wsModule.redactWsUrl('not a url'), '<redacted>');
