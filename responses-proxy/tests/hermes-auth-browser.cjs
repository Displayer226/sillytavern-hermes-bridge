const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const webpack = require('webpack');
const { chromium } = require('playwright');

(async () => {
    const root = path.resolve(__dirname, '..');
    const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-auth-browser-'));
    const entry = path.join(temp, 'entry.js');
    fs.writeFileSync(entry, `
        import React from 'react';
        import { createRoot } from 'react-dom/client';
        import Banner from ${JSON.stringify(path.join(root, 'src/HermesAuthBanner.js'))};
        const context = { extensionSettings: { responsesProxy: {
            wsUrl: location.origin.replace('http', 'ws') + '/proxy-ws/ws', wsToken: 'synthetic-proxy-token'
        } } };
        createRoot(document.getElementById('root')).render(<React.StrictMode><Banner context={context} /></React.StrictMode>);
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
            response.setHeader('Content-Type', request.url === '/bundle.js' ? 'text/javascript; charset=utf-8' : 'text/html; charset=utf-8');
            response.end(request.url === '/bundle.js' ? bundle : '<div id="root"></div><script src="/bundle.js"></script>');
        });
        await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
        const origin = `http://127.0.0.1:${server.address().port}`;
        browser = await chromium.launch({ headless: true });
        const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
        await page.route('**/*', route => {
            if (new URL(route.request().url()).origin === origin) return route.continue();
            return route.abort();
        });
        let connected = false, ready = false, fail = false, expired = false, retryAt = null;
        await page.route('**/v1/hermes/auth/status', route => route.fulfill({ json: {
            web_login_enabled: true, login_required: !ready && !retryAt, connected, ready,
            session_expired: expired, retry_at: retryAt,
        } }));
        await page.route('**/v1/hermes/auth/login', async route => {
            assert.equal(route.request().method(), 'POST');
            assert.equal(route.request().headers().authorization, 'Bearer synthetic-proxy-token');
            assert.ok(!route.request().url().includes('synthetic-proxy-token'));
            await route.fulfill(fail ? { status: 503, json: { error: 'unavailable' } } : {
                json: { authorization_url: 'https://login.example.test/authorize?state=synthetic-state' },
            });
        });
        await page.route('https://login.example.test/**', route => {
            assert.equal(route.request().headers().authorization, undefined);
            assert.ok(!route.request().url().includes('synthetic-proxy-token'));
            return route.fulfill({ body: 'OIDC login destination' });
        });
        await page.goto(origin);
        const button = page.getByRole('button', { name: 'Connect Hermes', exact: true });
        await button.waitFor();
        const banner = await page.getByRole('status').boundingBox();
        assert.ok(banner.x >= 0 && banner.x + banner.width <= 390);
        await button.click();
        await page.waitForURL('https://login.example.test/**');
        connected = true; ready = true;
        await page.goto(origin);
        await page.waitForResponse('**/v1/hermes/auth/status');
        assert.equal(await page.getByRole('button', { name: 'Connect Hermes', exact: true }).count(), 0);
        connected = false; ready = false; fail = true;
        await page.goto(origin);
        await button.waitFor();
        await button.click();
        await page.getByRole('alert').waitFor();
        assert.ok(await button.isEnabled());
        expired = true; fail = false;
        await page.goto(origin);
        await page.getByRole('button', { name: 'Reconnect Hermes', exact: true }).waitFor();
        assert.ok(await page.getByText('Hermes session expired', { exact: false }).isVisible());
        retryAt = Math.floor(Date.now() / 1000) + 120;
        await page.goto(origin);
        await page.getByText('Hermes is temporarily unavailable.', { exact: false }).waitFor();
        assert.equal(await page.getByRole('button').count(), 0);
        console.log('PASS: Hermes browser login banner, authenticated start, redirect, ready state and retry');
    } finally {
        await browser?.close();
        if (server) await new Promise(resolve => server.close(resolve));
        fs.rmSync(temp, { recursive: true, force: true });
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
