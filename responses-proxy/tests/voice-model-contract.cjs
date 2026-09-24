/* Offline behavioral check for src/VoiceCall.js snapshot() model contract.
 * Extracts the real snapshot() function from the source by brace counting and
 * evaluates it with plain Node (the function is pure ESM, no JSX): no build,
 * no network, no dependencies needed. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '..', 'src', 'VoiceCall.js'), 'utf8');
const marker = 'function snapshot(ctx)';
const start = source.indexOf(marker);
assert(start >= 0, 'snapshot() not found in src/VoiceCall.js');
const bodyStart = source.indexOf('{', start);
let depth = 0;
let end = -1;
for (let i = bodyStart; i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}') { depth--; if (depth === 0) { end = i + 1; break; } }
}
assert(end > start, 'snapshot() braces did not balance');
const snapCode = source.slice(start, end);
assert(snapCode.endsWith('}'));

// snapshot() reads the module-level chatId helper; extract that real line too.
const chatIdLine = source.match(/^const chatId = .*$/m)?.[0];
assert(chatIdLine, 'chatId helper not found in src/VoiceCall.js');

const sandboxCtx = (model) => ({
    getCurrentChatId: () => 'mobile-chat',
    characters: [{ description: 'd', personality: 'p', scenario: 's' }],
    characterId: 0,
    extensionSettings: { responsesProxy: { selectedModel: model, profileByChat: { 'mobile-chat': 'local' } } },
    chat: [],
});
const ctxTrim = sandboxCtx('  test-model  ');
ctxTrim.powerUserSettings = { persona_description: 'persona' };

// The eval runs in this function's own scope so the eval'd function
// declaration cannot collide with module-level bindings.
const loadSnapshot = () => eval(`${chatIdLine}\n${snapCode}\nsnapshot;`);
const snapshot = loadSnapshot();

const withOverride = snapshot(sandboxCtx('test-model'));
assert.deepEqual(
    Object.keys(withOverride).sort(),
    ['messages', 'model', 'profile', 'session_id', 'workspace'],
);
assert.equal(withOverride.model, 'test-model');
assert.equal(withOverride.profile, 'local');
assert.equal(withOverride.session_id, 'mobile-chat');

const trimmed = snapshot(ctxTrim);
assert.equal(trimmed.model, 'test-model', 'override must be trimmed');

const noOverride = snapshot(sandboxCtx(''));
assert(!('model' in noOverride), 'empty override must omit the model field');
assert.deepEqual(
    Object.keys(noOverride).sort(),
    ['messages', 'profile', 'session_id', 'workspace'],
);
assert.equal(noOverride.profile, 'local');
assert.equal(noOverride.session_id, 'mobile-chat');

const nonString = snapshot(sandboxCtx(undefined));
assert(!('model' in nonString), 'non-string override must omit the model field');

console.log('PASS: snapshot() model contract (override kept/trimmed, empty omitted)');
