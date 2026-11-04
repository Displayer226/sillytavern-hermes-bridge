const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const webpack = require('webpack');
const { chromium } = require('playwright');

(async () => {
    const root = path.resolve(__dirname, '..');
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-root-'));
    const entry = path.join(temp, 'entry.js');
    const mock = path.join(temp, 'socket.js');
    const aliases = { [path.join(root, 'src/WebSocketService.js')]: mock };
    if (process.env.HERMES_BASELINE_BANNER) {
        const baseline = path.join(temp, 'baseline-banner.js');
        fs.writeFileSync(baseline, fs.readFileSync(process.env.HERMES_BASELINE_BANNER, 'utf8')
            .replace("'./ProxyHttp'", JSON.stringify(path.join(root, 'src/ProxyHttp.js')))
            .replace("'./hermesAuthState'", JSON.stringify(path.join(root, 'src/hermesAuthState.js'))));
        aliases[path.join(root, 'src/HermesAuthBanner.js')] = baseline;
    }
    fs.writeFileSync(mock, `export const deriveWsUrl = () => ''; class Socket { static deriveUrl() { return ''; } on() { return () => {}; } off() {} setContext() {} connect() {} disconnect() {} subscribe() {} send() {} } export default new Socket();`);
    fs.writeFileSync(entry, `
        import React from 'react';
        import { createRoot } from 'react-dom/client';
        import Root from ${JSON.stringify(path.join(root, 'src/ToolCallsPanelRoot.js'))};
        const timer = window.setTimeout; window.setTimeout = (fn, ms, ...args) => timer(fn, ms === 10000 ? 1000 : ms, ...args);
        const realFetch = window.fetch; window.fetch = (url, options) => realFetch(url, String(url).includes('/v1/hermes/auth/') ? { ...options, signal: undefined } : options);
        window.SillyTavern = { getContext: () => context, toastr: {} };
        const context = { extensionSettings: { responsesProxy: { wsUrl: location.origin.replace('http', 'ws') + '/a/ws', wsToken: 'old-synthetic' } }, chat: [], eventSource: { on() {}, removeListener() {} }, eventTypes: {} };
        window.change = (emit) => { context.extensionSettings.responsesProxy = { wsUrl: location.origin.replace('http', 'ws') + '/b/ws', wsToken: 'new-synthetic' }; if (emit) window.dispatchEvent(new CustomEvent('responses-proxy-settings-updated', { detail: context.extensionSettings.responsesProxy })); };
        createRoot(document.getElementById('root')).render(<Root context={context} />);
    `);
    let server, browser;
    try {
        await new Promise((resolve, reject) => webpack({ mode: 'development', entry, devtool: false,
            output: { path: temp, filename: 'bundle.js' },
            resolve: { modules: [path.join(root, 'node_modules'), 'node_modules'], alias: aliases },
            module: { rules: [{ test: /\.js$/, exclude: /node_modules/, loader: require.resolve('babel-loader'), options: { presets: [require.resolve('@babel/preset-env'), require.resolve('@babel/preset-react')] } }] },
        }, (error, stats) => error || stats.hasErrors() ? reject(error || new Error(stats.toString())) : resolve()));
        const bundle = fs.readFileSync(path.join(temp, 'bundle.js'));
        server = http.createServer((req, res) => { res.setHeader('Content-Type', req.url === '/bundle.js' ? 'text/javascript' : 'text/html'); res.end(req.url === '/bundle.js' ? bundle : '<div id="root"></div><script src="/bundle.js"></script>'); });
        await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
        const origin = `http://127.0.0.1:${server.address().port}`;
        browser = await chromium.launch({ headless: true });
        const page = await browser.newPage(); page.setDefaultTimeout(3000);
        const browserErrors = [];
        page.on('pageerror', error => browserErrors.push(`Browser error: ${error.message}`));
        page.on('console', message => { if (message.type() === 'error') browserErrors.push(`Browser console: ${message.text()}`); });
        let state = 'login', held, heldStatus, heldCurrentStatus, holdStatus = false, requests = [], retryAt = 1700000000, externalAttempts = 0;
        const waitFor = async (predicate, label) => {
            const deadline = Date.now() + 3000;
            while (!predicate()) {
                if (Date.now() >= deadline) throw new Error(`Timed out waiting for ${label}`);
                await new Promise(resolve => setTimeout(resolve, 10));
            }
        };
        const assertDrawerOpen = async () => {
            const drawer = page.locator('.rp-drawer');
            await drawer.waitFor({ state: 'visible' });
            await page.waitForFunction(() => {
                const drawer = document.querySelector('.rp-drawer');
                return drawer && Math.abs(new DOMMatrix(getComputedStyle(drawer).transform).m41) < 1;
            });
            assert.equal(await drawer.locator('.rp-drawer-title').innerText(), 'Agent Tool Calls');
            assert.equal(await page.getByText('Something went wrong in Hermes Bridge UI.').count(), 0);
        };
        await page.route('**/*', route => {
            const req = route.request(), url = new URL(req.url());
            if (url.origin !== origin) { externalAttempts++; return route.abort(); }
            if (url.pathname.endsWith('/v1/hermes/auth/status')) {
                requests.push([url.pathname, req.headers().authorization]);
                if (holdStatus && url.pathname.startsWith('/a/')) { heldStatus = route; return; }
                if (holdStatus && url.pathname.startsWith('/b/')) { heldCurrentStatus = route; return; }
                return route.fulfill({ json: { connected: state === 'ready', ready: state === 'ready', login_required: state === 'login', session_expired: false, retry_at: state === 'retry' ? retryAt : null, web_login_enabled: true } });
            }
            if (url.pathname.endsWith('/v1/hermes/auth/login')) { held = route; return; }
            return route.continue();
        });
        const choice = process.env.HERMES_REGRESSION || 'all';
        if (choice === 'scope' || choice === 'all') {
            await page.goto(origin); await page.getByRole('button', { name: 'Connect Hermes' }).waitFor();
            await page.getByRole('button', { name: 'Open Agent Tool Calls' }).click();
            await assertDrawerOpen();
            await page.evaluate(() => window.change(false)); await page.waitForTimeout(1300);
            assert.ok(!requests.some(([url, auth]) => url.startsWith('/a/') && auth === 'Bearer new-synthetic'), 'new token must never reach old URL');
            assert.ok(requests.some(([url, auth]) => url.startsWith('/b/') && auth === 'Bearer new-synthetic'), 'polling must resume with changed settings');
            await assertDrawerOpen();
            requests = []; await page.goto(origin); await page.getByRole('button', { name: 'Connect Hermes' }).waitFor();
            await page.evaluate(() => window.change(true));
            await page.waitForFunction(() => document.querySelector('.rp-voice-floating-btn'));
            await page.waitForTimeout(400);
            assert.ok(requests.some(([url, auth]) => url.startsWith('/b/') && auth === 'Bearer new-synthetic'), 'settings event must update the real root');
            requests = []; state = 'login'; heldStatus = null; heldCurrentStatus = null; holdStatus = false;
            await page.goto(origin); await page.getByRole('button', { name: 'Connect Hermes' }).waitFor();
            await page.getByRole('button', { name: 'Open Agent Tool Calls' }).click(); await assertDrawerOpen();
            holdStatus = true;
            await waitFor(() => heldStatus, 'old status request');
            assert.equal(heldStatus.request().headers().authorization, 'Bearer old-synthetic');
            await page.evaluate(() => window.change(false));
            state = 'ready';
            await heldStatus.fulfill({ json: { connected: false, ready: false, login_required: true, session_expired: false, retry_at: null, web_login_enabled: true } });
            await waitFor(() => heldCurrentStatus, 'current status request');
            assert.equal(heldCurrentStatus.request().headers().authorization, 'Bearer new-synthetic');
            assert.ok(!requests.some(([url, auth]) => url.startsWith('/a/') && auth === 'Bearer new-synthetic'), 'new token must never reach old URL during a request');
            assert.equal(await page.getByRole('button', { name: 'Connect Hermes' }).count(), 0, 'stale login status must not be displayed');
            await assertDrawerOpen();
            await heldCurrentStatus.fulfill({ json: { connected: true, ready: true, login_required: false, session_expired: false, retry_at: null, web_login_enabled: true } });
            await page.waitForFunction(() => !document.querySelector('[aria-label="Hermes connection"]'));
            assert.equal(page.url(), origin + '/'); assert.equal(externalAttempts, 0);
            await assertDrawerOpen(); holdStatus = false;
        }
        if (choice === 'login' || choice === 'all') {
            state = 'login'; await page.goto(origin); await page.getByRole('button', { name: 'Connect Hermes' }).click();
            await waitFor(() => held, 'login request');
            state = 'ready'; await page.waitForFunction(() => !document.querySelector('[aria-label="Hermes connection"]'));
            await held.fulfill({ json: { authorization_url: 'https://login.example.test/authorize' } }).catch(() => {});
            await page.waitForTimeout(100);
            state = 'login'; const button = page.locator('aside[aria-label="Hermes connection"] button'); await button.waitFor();
            assert.ok(await button.isEnabled(), 'login button must recover after stale successful login');
            assert.equal(page.url(), origin + '/');
            held = null; await button.click();
            await waitFor(() => held, 'second login request');
            state = 'ready'; await page.waitForFunction(() => !document.querySelector('[aria-label="Hermes connection"]'));
            state = 'login'; await button.waitFor(); assert.ok(await button.isEnabled());
            await held.fulfill({ json: { authorization_url: 'https://login.example.test/authorize' } });
            await page.waitForTimeout(100);
            assert.equal(page.url(), origin + '/', 'a stale login must not redirect after login becomes required again');
            assert.ok(await button.isEnabled());
        }
        if (choice === 'retry' || choice === 'all') {
            state = 'retry'; await page.goto(origin);
            const banner = page.getByRole('status', { name: 'Hermes connection' }); await banner.waitFor();
            await page.getByText(/Next connection attempt/).waitFor();
            const expected = await page.evaluate(value => new Date(value * 1000).toLocaleString(), retryAt);
            assert.ok((await banner.innerText()).includes(expected), 'scheduled retry must show its date and time');
        }
        assert.equal(await page.getByText('Something went wrong in Hermes Bridge UI.').count(), 0);
        assert.deepEqual(browserErrors, [], 'browser errors and ErrorBoundary fallbacks must fail the test');
        console.log('PASS: real root Hermes authentication regressions');
    } finally { await browser?.close(); if (server) await new Promise(resolve => server.close(resolve)); fs.rmSync(temp, { recursive: true, force: true }); }
})().catch(error => { console.error(error); process.exitCode = 1; });
