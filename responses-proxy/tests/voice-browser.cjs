/* Builds the real call panel and verifies layout and start errors without
 * microphone access, tokens, or running a production Hermes action. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const webpack = require('webpack');
const { chromium } = require('playwright');

(async () => {
    const manifest = JSON.parse(await fs.readFile(path.join(__dirname, '..', 'manifest.json'), 'utf8'));
    assert.equal(manifest.js, `dist/index.js?v=${manifest.version}`, 'Extension entry is not cache-busted by its manifest version');
    const output = await fs.mkdtemp(path.join(os.tmpdir(), 'rp-voice-ui-'));
    const config = require('../webpack.config')({}, { mode: 'development' });
    config.entry = path.join(__dirname, 'voice-harness.js');
    config.output.path = output;
    config.output.publicPath = '/';
    await new Promise((resolve, reject) => webpack(config, (error, stats) => {
        if (error || stats.hasErrors()) reject(error || new Error(stats.toString())); else resolve();
    }));
    const builtFiles = await fs.readdir(output);
    assert(builtFiles.some((name) => /^voice-livekit\.[0-9a-f]{8}\.js$/.test(name)), 'Dynamic chunks are not cache-busted');
    const browser = await chromium.launch({ headless: true,
        ...(process.env.CHROMIUM_EXECUTABLE ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {}) });
    try {
        for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }, { width: 320, height: 568 }]) {
            const isTouch = viewport.width <= 390;
            const context = await browser.newContext({ viewport, hasTouch: isTouch });
            const page = await context.newPage();
            page.setDefaultTimeout(5000);
            const errors = [];
            let requestBody;
            page.on('pageerror', (err) => errors.push(err.message));
            await page.route('https://voice-test.invalid/**', async (route) => {
                const url = new URL(route.request().url());
                if (url.pathname === '/') return route.fulfill({ contentType: 'text/html; charset=utf-8', body: `<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body style="margin:0"><div id="root"></div><div id="form_sheld" style="display:flex;position:fixed;left:0;right:0;bottom:0;height:64px;background:#222"><div id="send_form" style="width:100%;height:64px"></div></div><script src="/index.js?v=${manifest.version}"></script></body></html>` });
                if (url.pathname === '/v1/voice/calls') {
                    requestBody = route.request().postDataJSON();
                    return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: 'An active call already owns this chat' }) });
                }
                if (url.pathname.endsWith('.js')) return route.fulfill({ contentType: 'text/javascript; charset=utf-8', body: await fs.readFile(path.join(output, path.basename(url.pathname))) });
                return route.abort();
            });
            await page.addInitScript(() => {
                Object.defineProperty(navigator, 'mediaDevices', { value: { getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }) } });
            });
            await page.goto('https://voice-test.invalid/');
            await page.evaluate(() => {
                window.testVoiceDelta({ call_id: 'call-1', turn_id: 'turn-1', text: 'Bonjour ' });
                window.testVoiceDelta({ call_id: 'call-1', turn_id: 'turn-1', text: 'à tous' });
            });
            assert.equal(await page.evaluate(() => window.testVoiceChat.length), 1);
            assert.equal(await page.evaluate(() => window.testVoiceChat[0].mes), 'Bonjour à tous');
            assert.equal(await page.evaluate(() => window.testVoiceChat[0].extra.voice_stream_pending), true);
            await page.evaluate(() => window.testVoiceFinal(
                { call_id: 'call-1', role: 'assistant', text: 'Bonjour à tous !', interrupted: false },
                'call-1:message-1',
            ));
            assert.equal(await page.evaluate(() => window.testVoiceChat.length), 1, 'Final event duplicated streamed bubble');
            assert.equal(await page.evaluate(() => window.testVoiceChat[0].mes), 'Bonjour à tous !');
            assert.equal(await page.evaluate(() => window.testVoiceChat[0].extra.voice_id), 'call-1:message-1');
            assert.equal(await page.evaluate(() => window.testVoiceChat[0].extra.voice_stream_pending), undefined);
            await page.evaluate(() => {
                window.testVoiceDelta({ call_id: 'call-2', turn_id: 'abandoned-turn', text: 'Bien sûr ! Tout ' });
                window.testVoiceDelta({ call_id: 'call-2', turn_id: 'final-turn', text: 'Bien sûr ! Tout semble ' });
            });
            await page.evaluate(() => window.testVoiceFinal(
                { call_id: 'call-2', role: 'assistant', text: 'Bien sûr ! Tout semble correct ?', interrupted: false },
                'call-2:message-2',
            ));
            assert.equal(await page.evaluate(() => window.testVoiceChat.length), 2, 'Orphan streamed bubble was retained');
            assert.equal(await page.evaluate(() => window.testVoiceChat[1].mes), 'Bien sûr ! Tout semble correct ?');
            assert.equal(await page.evaluate(() => window.testVoiceChat[1].extra.voice_stream_pending), undefined);
            const trigger = page.getByRole('button', { name: 'Appeler Hermes' });
            await trigger.waitFor();
            const initialTriggerBox = await trigger.boundingBox();
            assert(initialTriggerBox.x >= 0 && initialTriggerBox.x + initialTriggerBox.width <= viewport.width, 'Voice trigger overflows horizontally');
            assert(initialTriggerBox.y >= 0 && initialTriggerBox.y + initialTriggerBox.height <= viewport.height, 'Voice trigger overflows vertically');
            assert(initialTriggerBox.height >= 44 && initialTriggerBox.width >= 44, 'Voice trigger touch target too small');
            if (viewport.width <= 390 && viewport.height >= 800) {
                const stackOrder = async () => page.locator('.rp-voice-floating-btn, .rp-test-peer').evaluateAll((elements) => elements
                    .map((element) => ({
                        name: element.getAttribute('data-floating-name') || 'voice',
                        y: element.getBoundingClientRect().top,
                        bottom: element.getBoundingClientRect().bottom,
                    }))
                    .sort((left, right) => left.y - right.y));
                const restingStack = await stackOrder();
                await page.setViewportSize({ width: viewport.width, height: 500 });
                await page.waitForTimeout(100);
                const keyboardTriggerBox = await trigger.boundingBox();
                const sendFormBox = await page.locator('#form_sheld').boundingBox();
                assert(
                    keyboardTriggerBox.y + keyboardTriggerBox.height + 10 <= sendFormBox.y,
                    'Voice trigger overlaps the send form while the keyboard is open',
                );
                const keyboardStack = await stackOrder();
                assert.deepEqual(
                    keyboardStack.map(({ name }) => name),
                    restingStack.map(({ name }) => name),
                    'Floating button order changed while the keyboard opened',
                );
                for (let index = 1; index < keyboardStack.length; index++) {
                    assert(
                        keyboardStack[index - 1].bottom + 7 <= keyboardStack[index].y,
                        `Floating buttons overlap while the keyboard is open: ${JSON.stringify(keyboardStack)}`,
                    );
                }
                await trigger.dragTo(page.locator('body'), { targetPosition: { x: 180, y: 80 } });
                await page.setViewportSize(viewport);
                await page.waitForTimeout(300);
                const restoredTriggerBox = await trigger.boundingBox();
                assert(
                    Math.abs(restoredTriggerBox.y - initialTriggerBox.y) <= 1,
                    `Voice trigger did not return after keyboard close (${initialTriggerBox.y} -> ${restoredTriggerBox.y})`,
                );
                await trigger.tap();
                await page.getByRole('dialog').waitFor();
                await page.getByRole('button', { name: 'Collapse call panel' }).tap();
            }
            await trigger.dragTo(page.locator('body'), { targetPosition: { x: Math.floor(viewport.width / 2), y: 60 } });
            const draggedTriggerBox = await trigger.boundingBox();
            assert(draggedTriggerBox.y < initialTriggerBox.y, 'Voice trigger was not draggable');
            assert(await page.evaluate(() => Boolean(localStorage.getItem('responses-proxy-floating-voice'))), 'Voice trigger position was not persisted');
            if (isTouch) await trigger.tap();
            else await trigger.click();
            const dialog = page.getByRole('dialog');
            await dialog.waitFor();
            const box = await dialog.boundingBox();
            assert(box.x >= 0 && box.x + box.width <= viewport.width, `Panel overflows at ${viewport.width}px`);
            assert(box.y >= 0 && box.y + box.height <= viewport.height, 'Panel overflows vertically');
            for (const button of await dialog.getByRole('button').all()) {
                const buttonBox = await button.boundingBox();
                assert(
                    buttonBox.height >= 44,
                    `Touch target too small: ${await button.getAttribute('aria-label') || await button.textContent()} (${buttonBox.height}px)`,
                );
            }
            await page.getByRole('button', { name: 'Start call' }).click();
            await page.getByRole('alert').filter({ hasText: 'An active call already owns this chat' }).waitFor();
            assert.equal(requestBody.session_id, 'mobile-chat');
            assert.equal(requestBody.model, 'test-model');
            assert.equal(requestBody.profile, 'local');
            assert(requestBody.messages[0].content.includes('Hermes'));
            // With no model override, the request omits the model field so the
            // call inherits the backend's current model (same contract as text
            // requests). No hard-coded default belongs in the extension.
            await page.evaluate(() => window.testSetVoiceModel(''));
            requestBody = undefined;
            await page.getByRole('button', { name: 'Start call' }).click();
            await page.getByRole('alert').filter({ hasText: 'An active call already owns this chat' }).waitFor();
            assert.equal(requestBody.session_id, 'mobile-chat');
            assert.equal(requestBody.model, undefined);
            assert.equal(requestBody.profile, 'local');
            assert.equal(context.pages().length, 1, 'Voice opened another tab');
            await page.screenshot({ path: path.join(output, `voice-${viewport.width}.png`) });
            await page.keyboard.press('Escape');
            assert.equal(await dialog.count(), 0);
            assert.deepEqual(errors, []);
            console.log(`PASS: ${viewport.width}x${viewport.height}, touch targets, same tab, session routing, error recovery, Escape`);
            await context.close();
        }
        console.log(`Screenshots: ${output}`);
    } finally { await browser.close(); }
})().catch((err) => { console.error(err); process.exitCode = 1; });
