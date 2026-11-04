const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const webpack = require('webpack');
const { chromium } = require('playwright');

(async () => {
    const root = path.resolve(__dirname, '..');
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-auth-states-'));
    const entry = path.join(temp, 'entry.js');
    fs.writeFileSync(entry, `
        import React from 'react';
        import { createRoot } from 'react-dom/client';
        import Banner from ${JSON.stringify(path.join(root, 'src/HermesAuthBanner.js'))};
        const root = createRoot(document.getElementById('root'));
        const context = { extensionSettings: { responsesProxy: {
            wsUrl: location.origin.replace('http', 'ws') + '/a/ws', wsToken: 'synthetic-proxy-token'
        } } };
        window.renderBanner = () => root.render(<React.StrictMode><Banner context={context} /></React.StrictMode>);
        window.changeConnection = () => { context.extensionSettings.responsesProxy.wsUrl = location.origin.replace('http', 'ws') + '/b/ws'; window.renderBanner(); };
        window.mutateConnection = () => { context.extensionSettings.responsesProxy.wsUrl = location.origin.replace('http', 'ws') + '/b/ws'; };
        window.changeToken = () => { context.extensionSettings.responsesProxy.wsToken = 'synthetic-new-token'; window.renderBanner(); };
        window.unmountBanner = () => root.unmount();
        window.renderBanner();
    `);
    let server, browser;
    try {
        await new Promise((resolve, reject) => webpack({
            mode: 'development', entry, devtool: false, output: { path: temp, filename: 'bundle.js' },
            resolve: { modules: [path.join(root, 'node_modules'), 'node_modules'] },
            module: { rules: [{ test: /\.js$/, exclude: /node_modules/, loader: require.resolve('babel-loader'),
                options: { presets: [require.resolve('@babel/preset-env'), require.resolve('@babel/preset-react')] } }] },
        }, (error, stats) => error || stats.hasErrors() ? reject(error || new Error(stats.toString())) : resolve()));
        const bundle = fs.readFileSync(path.join(temp, 'bundle.js'));
        server = http.createServer((request, response) => {
            response.setHeader('Content-Type', request.url === '/bundle.js' ? 'text/javascript' : 'text/html');
            response.end(request.url === '/bundle.js' ? bundle : '<div id="root"></div><script src="/bundle.js"></script>');
        });
        await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
        const origin = `http://127.0.0.1:${server.address().port}`;
        browser = await chromium.launch({ headless: true });
        const page = await browser.newPage();
        page.setDefaultTimeout(3000);
        const baseline = { connected: false, ready: false, login_required: false,
            session_expired: false, retry_at: null, web_login_enabled: true };
        let fixture = { ...baseline }, statusCode = 200, statusFail = false, loginCode = 503, loginCount = 0, loginTarget = null;
        let expectedToken = 'synthetic-proxy-token';
        let heldLogin = null, heldStatus = null, holdOld = false, externalAttempts = 0;
        await page.route('**/*', async route => {
            const request = route.request();
            const url = new URL(request.url());
            if (url.origin !== origin) { externalAttempts++; return route.abort(); }
            if (url.pathname.endsWith('/v1/hermes/auth/status')) {
                assert.equal(request.headers().authorization, `Bearer ${expectedToken}`);
                if (statusFail) return route.abort();
                if (holdOld && url.pathname.startsWith('/a/')) { heldStatus = route; return; }
                return route.fulfill({ status: statusCode, json: fixture });
            }
            if (url.pathname.endsWith('/v1/hermes/auth/login')) {
                assert.equal(request.headers().authorization, 'Bearer synthetic-proxy-token');
                assert.ok(!request.url().includes('synthetic-proxy-token'));
                loginCount++;
                if (heldLogin === 'hold') { heldLogin = route; return; }
                return route.fulfill({ status: loginCode, json: loginTarget
                    ? { authorization_url: loginTarget } : { error: 'synthetic-private-error' } });
            }
            return route.continue();
        });
        async function show(overrides, code = 200) {
            fixture = { ...baseline, ...overrides }; statusCode = code; statusFail = false; expectedToken = 'synthetic-proxy-token';
            await page.goto(origin);
            await page.waitForResponse(response => response.url().includes('/v1/hermes/auth/status'));
        }
        const banner = page.getByRole('status', { name: 'Hermes connection' });
        await show({ connected: true });
        await page.getByText('Hermes gateway is starting or reconnecting.').waitFor();
        await show({ connected: true, ready: true });
        await page.waitForTimeout(50);
        assert.equal(await banner.count(), 0);
        await show({ web_login_enabled: false, connected: true });
        await banner.waitFor();
        assert.equal(await page.getByRole('button', { name: /Connect|Reconnect/ }).count(), 0);
        await show({ login_required: true });
        await page.getByRole('button', { name: 'Connect Hermes' }).waitFor();
        await show({ login_required: true, session_expired: true });
        await page.getByRole('button', { name: 'Reconnect Hermes' }).waitFor();
        await show({ retry_at: 1 });
        await page.getByText(/Next connection attempt/).waitFor();
        assert.equal(await page.getByRole('button', { name: /Connect|Reconnect/ }).count(), 0);
        await show({}, 401);
        await page.getByText('Hermes status is unavailable.').waitFor();
        assert.equal(await page.getByRole('button', { name: /Connect|Reconnect/ }).count(), 0);
        await show({}, 403);
        await page.getByText('Hermes status is unavailable.').waitFor();
        statusFail = true;
        await page.goto(origin);
        await page.getByText('Hermes status is unavailable.').waitFor();
        statusFail = false;
        fixture = { unexpected: true }; statusCode = 200;
        await page.goto(origin);
        await page.getByText('Hermes status is unavailable.').waitFor();
        fixture = null;
        await page.goto(origin);
        await page.getByText('Hermes status is unavailable.').waitFor();
        await show({ login_required: true });
        heldLogin = 'hold'; loginCount = 0;
        const button = page.getByRole('button', { name: 'Connect Hermes' });
        await button.waitFor();
        await button.dblclick({ timeout: 2000 }).catch(() => {});
        await page.waitForTimeout(100);
        assert.equal(loginCount, 1);
        await heldLogin.fulfill({ status: 503, json: { error: 'synthetic-private-error' } });
        await page.getByRole('alert').waitFor();
        assert.ok(!(await page.getByRole('alert').innerText()).includes('synthetic-private-error'));
        assert.ok(await button.isEnabled());
        await button.click();
        assert.equal(loginCount, 2);
        for (const target of ['http://login.example.test/', 'https://user:password@login.example.test/',
            'https://login.example.test/?token=synthetic-proxy-token']) {
            loginTarget = target; loginCode = 200;
            await Promise.all([
                page.waitForResponse(response => response.url().includes('/v1/hermes/auth/login')),
                button.click(),
            ]);
            await page.getByRole('alert').waitFor();
            assert.equal(page.url(), origin + '/');
            assert.ok(await button.isEnabled());
        }
        holdOld = true; heldStatus = null; fixture = { ...baseline, connected: true, ready: true };
        await page.goto(origin);
        await page.waitForFunction(() => typeof window.changeConnection === 'function');
        await page.waitForTimeout(100);
        await page.evaluate(() => window.changeConnection());
        await page.waitForResponse(response => response.url().includes('/b/v1/hermes/auth/status'));
        assert.equal(await banner.count(), 0);
        if (heldStatus) await heldStatus.fulfill({ json: { ...baseline, login_required: true } }).catch(() => {});
        await page.waitForTimeout(100);
        assert.equal(await banner.count(), 0);
        await page.evaluate(() => window.unmountBanner());
        holdOld = true; heldStatus = null;
        await page.goto(origin);
        await page.waitForFunction(() => typeof window.unmountBanner === 'function');
        await page.waitForTimeout(100);
        await page.evaluate(() => window.unmountBanner());
        if (heldStatus) await heldStatus.fulfill({ json: { ...baseline, login_required: true } }).catch(() => {});
        await page.waitForTimeout(100);
        assert.equal(await banner.count(), 0);
        holdOld = true; heldStatus = null;
        await page.goto(origin);
        await page.waitForFunction(() => typeof window.mutateConnection === 'function');
        await page.waitForTimeout(100);
        await page.evaluate(() => window.mutateConnection());
        const newStatus = page.waitForResponse(response => response.url().includes('/b/v1/hermes/auth/status'));
        await heldStatus.fulfill({ json: { ...baseline, login_required: true } }).catch(() => {});
        await newStatus;
        assert.equal(await page.getByRole('button', { name: 'Connect Hermes' }).count(), 0);
        holdOld = false;
        await show({ login_required: true });
        fixture = { ...baseline, connected: true, ready: true };
        expectedToken = 'synthetic-new-token';
        const changedToken = page.waitForResponse(response => response.url().includes('/a/v1/hermes/auth/status'));
        await page.evaluate(() => window.changeToken());
        await changedToken;
        assert.equal(await page.getByRole('button', { name: 'Connect Hermes' }).count(), 0);
        holdOld = false; loginTarget = null; loginCode = 503;
        await show({ login_required: true });
        heldLogin = 'hold';
        await page.getByRole('button', { name: 'Connect Hermes' }).click();
        await page.waitForTimeout(100);
        fixture = { ...baseline, connected: true, ready: true };
        const changed = page.waitForResponse(response => response.url().includes('/b/v1/hermes/auth/status'));
        await page.evaluate(() => window.changeConnection());
        await changed;
        await heldLogin.fulfill({ json: { authorization_url: 'https://login.example.test/authorize' } }).catch(() => {});
        await page.waitForTimeout(100);
        assert.equal(page.url(), origin + '/');
        assert.equal(await banner.count(), 0);
        await show({ login_required: true });
        heldLogin = 'hold';
        await page.getByRole('button', { name: 'Connect Hermes' }).click();
        await page.waitForTimeout(100);
        await page.evaluate(() => window.unmountBanner());
        await heldLogin.fulfill({ json: { authorization_url: 'https://login.example.test/authorize' } }).catch(() => {});
        await page.waitForTimeout(100);
        assert.equal(page.url(), origin + '/');
        assert.equal(await banner.count(), 0);
        await show({ login_required: true });
        await page.waitForTimeout(9000);
        heldLogin = 'hold';
        await page.getByRole('button', { name: 'Connect Hermes' }).click();
        await page.waitForTimeout(100);
        fixture = { ...baseline, connected: true, ready: true };
        await page.waitForResponse(response => response.url().includes('/a/v1/hermes/auth/status'), { timeout: 12000 });
        await heldLogin.fulfill({ json: { authorization_url: 'https://login.example.test/authorize' } }).catch(() => {});
        await page.waitForTimeout(200);
        assert.equal(page.url(), origin + '/');
        assert.equal(externalAttempts, 0);
        assert.equal(await banner.count(), 0);
        for (const [width, height] of [[1440, 900], [390, 844], [320, 568]]) {
            await page.setViewportSize({ width, height });
            holdOld = false;
            await show({ login_required: true });
            const box = await banner.boundingBox();
            assert.ok(box && box.x >= 0 && box.y >= 0 && box.x + box.width <= width && box.y + box.height <= height);
        }
        await show({ connected: true, ready: true });
        await page.waitForTimeout(100);
        statusCode = 503;
        await page.getByText('Hermes status is unavailable.').waitFor({ timeout: 12000 });
        console.log('PASS: Hermes banner states, lifecycle, actions and viewports');
    } finally {
        await browser?.close();
        if (server) await new Promise(resolve => server.close(resolve));
        fs.rmSync(temp, { recursive: true, force: true });
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
