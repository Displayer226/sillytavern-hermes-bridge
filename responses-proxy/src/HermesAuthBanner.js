import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { proxyAuthToken, proxyHttpBaseUrl } from './ProxyHttp';
import { presentHermesAuth } from './hermesAuthState';

const POLL_MS = 10000;
const TIMEOUT_MS = 8000;
const LOGIN_ERROR = 'Hermes sign-in could not be started. Please try again.';

export default function HermesAuthBanner({ context }) {
    const [, refreshScope] = useState(0);
    const base = proxyHttpBaseUrl(context);
    const token = proxyAuthToken(context);
    const scopeRef = useRef(null);
    if (!scopeRef.current || scopeRef.current.context !== context
        || scopeRef.current.base !== base || scopeRef.current.token !== token) {
        scopeRef.current = { context, base, token, id: {} };
    }
    const scope = scopeRef.current.id;
    const [result, setResult] = useState({ scope, status: null });
    const currentStatus = useRef({ scope, status: null });
    const [loginState, setLoginState] = useState({ scope, busy: false, error: '' });
    const busyRef = useRef(false);
    const loginController = useRef(null);
    const activeRef = useRef(false);
    const isCurrent = () => activeRef.current && scopeRef.current?.id === scope
        && proxyHttpBaseUrl(context) === base && proxyAuthToken(context) === token;

    useEffect(() => {
        const update = () => refreshScope(value => value + 1);
        window.addEventListener('responses-proxy-settings-updated', update);
        return () => window.removeEventListener('responses-proxy-settings-updated', update);
    }, []);

    useEffect(() => {
        let disposed = false;
        let timer;
        let timeout;
        let controller;
        activeRef.current = true;
        busyRef.current = false;
        currentStatus.current = { scope, status: null };
        setResult({ scope, status: null });
        setLoginState({ scope, busy: false, error: '' });
        const invalidateLogin = () => {
            if (!loginController.current) return;
            loginController.current.abort();
            loginController.current = null;
            busyRef.current = false;
            setLoginState({ scope, busy: false, error: '' });
        };
        const check = async () => {
            if (disposed) return;
            if (!isCurrent()) { refreshScope(value => value + 1); return; }
            controller = new AbortController();
            timeout = setTimeout(() => controller.abort(), TIMEOUT_MS);
            try {
                if (!base) throw new Error('No proxy address');
                const response = await fetch(`${base}/v1/hermes/auth/status`, {
                    signal: controller.signal, cache: 'no-store', headers: token ? { Authorization: `Bearer ${token}` } : {},
                });
                if (!response.ok) throw new Error('Status unavailable');
                const value = await response.json();
                if (!disposed && isCurrent()) {
                    const status = value === null ? undefined : value;
                    currentStatus.current = { scope, status };
                    if (!presentHermesAuth(status).action) invalidateLogin();
                    setResult({ scope, status });
                }
            } catch (_) {
                if (!disposed && isCurrent()) {
                    currentStatus.current = { scope, status: undefined };
                    invalidateLogin();
                    setResult({ scope, status: undefined });
                }
            } finally {
                clearTimeout(timeout);
                if (!disposed) {
                    if (!isCurrent()) refreshScope(value => value + 1);
                    else timer = setTimeout(check, POLL_MS);
                }
            }
        };
        check();
        return () => {
            disposed = true;
            activeRef.current = false;
            clearTimeout(timer);
            clearTimeout(timeout);
            controller?.abort();
            loginController.current?.abort();
        };
    }, [scope]);

    const connect = async () => {
        const canLogin = () => isCurrent() && currentStatus.current.scope === scope
            && Boolean(presentHermesAuth(currentStatus.current.status).action);
        if (busyRef.current || !canLogin()) return;
        busyRef.current = true;
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), TIMEOUT_MS);
        loginController.current = controller;
        setLoginState({ scope, busy: true, error: '' });
        try {
            if (!canLogin()) return;
            const response = await fetch(`${base}/v1/hermes/auth/login`, {
                method: 'POST', credentials: 'include', signal: controller.signal,
                headers: token ? { Authorization: `Bearer ${token}` } : {},
            });
            if (!response.ok) throw new Error('Login unavailable');
            const result = await response.json();
            const target = new URL(result.authorization_url);
            if (target.protocol !== 'https:' || target.username || target.password
                || [...target.searchParams.keys()].some(key => /^(access_token|refresh_token|token)$/i.test(key))
                || (token && (target.href.includes(token) || decodeURIComponent(target.href).includes(token)))) {
                throw new Error('Invalid login destination');
            }
            if (loginController.current === controller && canLogin()) window.location.assign(target.href);
        } catch (_) {
            if (loginController.current === controller && isCurrent()) setLoginState({ scope, busy: false, error: LOGIN_ERROR });
        } finally {
            clearTimeout(timeout);
            if (loginController.current === controller) {
                if (isCurrent()) {
                    busyRef.current = false;
                    setLoginState(state => state.scope === scope ? { ...state, busy: false } : state);
                }
                loginController.current = null;
            }
        }
    };

    const view = presentHermesAuth(result.scope === scope ? result.status : null);
    if (view.kind === 'ready') return null;
    const messages = {
        initializing: 'Waiting for the Hermes connection.',
        unavailable: 'Hermes status is unavailable.',
        gateway_waiting: 'Hermes gateway is starting or reconnecting.',
        login_required: view.action ? 'Connect Hermes to continue.' : 'Hermes sign-in is required; browser sign-in is unavailable.',
        expired: view.action ? 'Hermes session expired. Reconnect to continue.' : 'Hermes session expired; browser sign-in is unavailable.',
        retrying: `Hermes is temporarily unavailable. Next connection attempt is scheduled for ${new Date(view.retryAt * 1000).toLocaleString()}.`,
    };
    const login = loginState.scope === scope ? loginState : { busy: false, error: '' };
    return createPortal(
        <aside aria-label="Hermes connection" role="status" style={{
            position: 'fixed', bottom: 24, left: 24, zIndex: 10001,
            maxWidth: 'min(380px, calc(100vw - 48px))', padding: 16,
            boxSizing: 'border-box', borderRadius: 12, background: 'var(--SmartThemeBlurTintColor, #202126)',
            color: 'var(--SmartThemeBodyColor, #fff)', border: '1px solid var(--SmartThemeBorderColor, #555)',
            boxShadow: '0 4px 24px #0005', backdropFilter: 'blur(12px)',
        }}>
            <div style={{ marginBottom: view.action ? 10 : 0 }}>{messages[view.kind]}</div>
            {view.action && <button type="button" className="menu_button" disabled={login.busy} onClick={connect}>
                {login.busy ? 'Connecting…' : (view.action === 'reconnect' ? 'Reconnect Hermes' : 'Connect Hermes')}
            </button>}
            {login.error && <div role="alert" style={{ marginTop: 10 }}>{login.error}</div>}
        </aside>, document.body,
    );
}
