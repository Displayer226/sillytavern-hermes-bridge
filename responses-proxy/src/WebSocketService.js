/**
 * WebSocketService — singleton that manages the persistent WebSocket connection
 * to the proxy-ST backend. Provides event-based notifications for real-time
 * tool call updates, session info, etc.
 *
 * Usage:
 *   import wsService from './WebSocketService';
 *   wsService.setContext(context);
 *   wsService.connect(sessionId);
 *   wsService.on('tool_call_added', (data) => { ... });
 *   wsService.send({ type: 'clear_tool_calls', session_id: '...' });
 *
 * Authentication contract: the token is never placed in the URL. It travels
 * in the second requested subprotocol, auth.<base64url(UTF-8(token))>, while
 * the server selects only the fixed application subprotocol
 * sillytavern-hermes-bridge.
 */

// ─── Standalone utility functions ──────────────────────────────────────

/**
 * Determine if a hostname is unreachable from the browser.
 * Only true for Docker-internal service names (no dots and not localhost).
 *
 * localhost, LAN IPs, Tailscale IPs, and public domains are all reachable.
 *
 * @param {string} url
 * @returns {boolean}
 */
export function isBrowserUnreachable(url) {
    try {
        const hostname = new URL(url).hostname;
        if (hostname === 'localhost' || hostname === '127.0.0.1' || hostname === '::1') return false;
        // Docker service names have no dots (e.g. "sillytavern-session-proxy")
        if (!hostname.includes('.')) return true;
        return false;
    } catch (e) {
        return true;
    }
}

/**
 * Encode a UTF-8 string as unpadded base64url (RFC 4648 §5).
 * TextEncoder guarantees UTF-8, so non-ASCII tokens round-trip.
 *
 * @param {string} value
 * @returns {string}
 */
export function encodeBase64UrlUtf8(value) {
    const bytes = new TextEncoder().encode(value);
    let binary = '';
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/**
 * Build the authentication subprotocol for the WebSocket handshake:
 * auth.<base64url(UTF-8(token))>, unpadded base64url.
 *
 * @param {string} token
 * @returns {string}
 */
export function buildAuthProtocol(token) {
    return `auth.${encodeBase64UrlUtf8(token)}`;
}

/**
 * Fixed application subprotocol selected by the server. The auth subprotocol
 * carries the token; the server never echoes it back.
 */
export const WS_APPLICATION_PROTOCOL = 'sillytavern-hermes-bridge';

/**
 * Redact a WebSocket URL for logging: keep scheme/host/path, drop the query
 * string so the token parameter and its value can never reach the console.
 *
 * @param {string|null|undefined} url
 * @returns {string}
 */
export function redactWsUrl(url) {
    if (typeof url !== 'string' || !url) return '<none>';
    try {
        const parsed = new URL(url);
        return `${parsed.protocol}//${parsed.host}${parsed.pathname}`;
    } catch (e) {
        // Unparseable URL: show a generic label instead of risking a leak.
        return '<redacted>';
    }
}

/**
 * Derive the WebSocket URL from SillyTavern context. Strategy:
 *
 *   1. Manual override from extension settings (wsUrl).
 *   2. Convert the ST custom_url/reverse_proxy from http→ws (if browser-reachable).
 *   3. If custom_url uses a Docker-internal hostname (unreachable from browser),
 *      fall back to the browser's current host + /proxy-ws/ws (reverse proxy route).
 *   4. Last resort: same host + /proxy-ws/ws.
 *
 * Authentication is never carried in the URL: the token travels in the
 * auth.* WebSocket subprotocol (see buildAuthProtocol).
 *
 * @param {object} context  SillyTavern context object
 * @returns {string}
 */
export function deriveWsUrl(context) {
    // Gather extension settings early (for wsUrl override)
    const extSettings = context.extensionSettings?.responsesProxy || {};

    // 1. Manual override
    if (extSettings.wsUrl && extSettings.wsUrl.trim()) {
        return extSettings.wsUrl.trim();
    }

    try {
        const settings = context.chatCompletionSettings || {};
        let baseUrl = settings.chat_completion_source === 'custom'
            ? settings.custom_url
            : settings.reverse_proxy || settings.custom_url;

        if (baseUrl) {
            baseUrl = baseUrl.trim().replace(/\/+$/, '');

            if (!isBrowserUnreachable(baseUrl)) {
                // 2. Convert http→ws, strip /v1, append /ws
                const wsUrl = baseUrl.replace(/^http/, 'ws').replace(/\/v1$/, '');
                return `${wsUrl}/ws`;
            }

            // 3. Docker-internal hostname: fall back to reverse proxy path
            console.info(
                '[Responses Proxy WS] custom_url "%s" is not browser-reachable, ' +
                'falling back to /proxy-ws/ws reverse proxy route',
                redactWsUrl(baseUrl),
            );
        }
    } catch (e) {
        console.warn('[Responses Proxy WS] Failed to derive URL from settings:', e);
    }

    // 4. Fallback: same host with /proxy-ws/ws (requires reverse proxy route)
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${window.location.host}/proxy-ws/ws`;
}

// ─── WebSocketService class ───────────────────────────────────────────

class WebSocketService {
    constructor() {
        /** @type {WebSocket|null} */
        this._ws = null;
        /** @type {string|null} */
        this._sessionId = null;
        /** @type {string|null} */
        this._url = null;
        /** @type {Map<string, Set<Function>>} */
        this._listeners = new Map();
        /** @type {number} */
        this._reconnectDelay = 1000;
        /** @type {number} */
        this._maxReconnectDelay = 30000;
        /** @type {ReturnType<typeof setTimeout>|null} */
        this._reconnectTimer = null;
        /** @type {boolean} */
        this._intentionalClose = false;
        /** @type {string|null} */
        this._connectionId = null;
        /** @type {boolean} */
        this._connected = false;
        /**
         * SillyTavern context reference, needed to read the proxy token for
         * the auth subprotocol. Set via setContext(); never logged.
         * @type {object|null}
         */
        this._context = null;
        /**
         * Auth subprotocol for the current connection attempt:
         * auth.<base64url(UTF-8(token))>. Embeds the secret — never log it.
         * @type {string|null}
         */
        this._authProtocol = null;
    }

    /**
     * Attach the SillyTavern context so the service can read the proxy token
     * when building the WebSocket handshake.
     * @param {object} context
     */
    setContext(context) {
        this._context = context || null;
    }

    /**
     * Determine if a hostname is unreachable from the browser.
     * Only true for Docker-internal service names (no dots and not localhost).
     *
     * localhost, LAN IPs, Tailscale IPs, and public domains are all reachable.
     */
    static _isBrowserUnreachable(url) {
        return isBrowserUnreachable(url);
    }

    /**
     * Derive the WebSocket URL (static wrapper for the standalone utility).
     */
    static deriveUrl(context) {
        return deriveWsUrl(context);
    }

    /**
     * Get the connection state.
     * @returns {boolean}
     */
    get isConnected() {
        return this._connected && this._ws?.readyState === WebSocket.OPEN;
    }

    /**
     * Get the connection ID assigned by the server.
     * @returns {string|null}
     */
    get connectionId() {
        return this._connectionId;
    }

    /**
     * Connect (or reconnect) to the proxy WebSocket, optionally subscribing to a session.
     * Keeping the socket open without a session lets global actions such as chat
     * deletion reach the proxy from SillyTavern's home screen.
     * @param {string|null|undefined} sessionId
     * @param {string} [url] Optional explicit WS URL
     */
    connect(sessionId, url) {
        const nextUrl = url || this._url;
        const nextSessionId = sessionId || null;
        if (!nextUrl) {
            console.warn('[Responses Proxy WS] Cannot connect: missing URL');
            return;
        }

        const nextAuthProtocol = this._computeAuthProtocol();
        const authChanged = nextAuthProtocol !== this._authProtocol;
        const socketIsActive = this._ws && (
            this._ws.readyState === WebSocket.CONNECTING ||
            this._ws.readyState === WebSocket.OPEN
        );

        // Both the extension bootstrap and the React panel may request the
        // connection. Reuse the in-flight/open socket so every server event is
        // delivered exactly once to the singleton listeners.
        if (socketIsActive && nextUrl === this._url) {
            if (authChanged) {
                // Auth identity changed (token edited): reopen the socket so
                // the next handshake carries the new subprotocol.
                this.disconnect();
                // Fall through to a fresh _doConnect() below.
            } else {
                if (this.isConnected && this._sessionId !== nextSessionId) {
                    this.subscribe(nextSessionId);
                } else {
                    this._sessionId = nextSessionId;
                }
                return;
            }
        }

        this._sessionId = nextSessionId;
        if (nextUrl !== this._url || authChanged) {
            this._url = nextUrl;
            this._reconnectDelay = 1000; // Reset backoff when connection target changes
        }
        this._authProtocol = nextAuthProtocol;

        this._intentionalClose = false;
        this._doConnect();
    }

    /**
     * Update the session subscription (e.g., when chat changes).
     * @param {string|null|undefined} sessionId
     */
    subscribe(sessionId) {
        const oldId = this._sessionId;
        const nextSessionId = sessionId || null;
        this._sessionId = nextSessionId;

        if (this.isConnected) {
            if (oldId && oldId !== nextSessionId) {
                this.send({ type: 'unsubscribe', session_id: oldId });
            }
            if (nextSessionId) {
                this.send({ type: 'subscribe', session_id: nextSessionId });
            }
        }
    }

    /**
     * Disconnect from the WebSocket.
     */
    disconnect() {
        this._intentionalClose = true;
        this._clearReconnectTimer();
        if (this._ws) {
            this._ws.close();
            this._ws = null;
        }
        this._connected = false;
    }

    /**
     * Send a JSON message to the server.
     * @param {object} msg
     */
    send(msg) {
        if (!this.isConnected) {
            console.warn('[Responses Proxy WS] Cannot send, not connected:', msg.type);
            return;
        }
        this._ws.send(JSON.stringify(msg));
    }

    /**
     * Register an event listener.
     * @param {string} event
     * @param {Function} callback
     * @returns {Function} Unsubscribe function
     */
    on(event, callback) {
        if (!this._listeners.has(event)) {
            this._listeners.set(event, new Set());
        }
        this._listeners.get(event).add(callback);

        // React panels are loaded lazily and may subscribe after the global
        // socket has already opened. Replay the current connected state so
        // their indicator cannot remain stale until the next reconnect.
        if (event === 'connected' && this.isConnected) {
            try {
                callback({ connection_id: this._connectionId });
            } catch (e) {
                console.error('[Responses Proxy WS] Connected listener error:', e);
            }
        }

        return () => {
            const set = this._listeners.get(event);
            if (set) {
                set.delete(callback);
            }
        };
    }

    /**
     * Remove an event listener.
     * @param {string} event
     * @param {Function} callback
     */
    off(event, callback) {
        const set = this._listeners.get(event);
        if (set) {
            set.delete(callback);
        }
    }

    // ─── Private ────────────────────────────────────────────────

    /**
     * Build the auth subprotocol from the current context token. Returns null
     * when no token is configured. Never log the result: it embeds the secret.
     *
     * @returns {string|null}
     */
    _computeAuthProtocol() {
        const token = this._context?.extensionSettings?.responsesProxy?.wsToken?.trim();
        return token ? buildAuthProtocol(token) : null;
    }

    _doConnect() {
        this._clearReconnectTimer();

        let socket;
        try {
            // Never log the raw URL or the auth subprotocol (it carries the token).
            console.log('[Responses Proxy WS] Connecting to %s', redactWsUrl(this._url));
            // The token travels in the auth.* subprotocol, never in the URL.
            // The server selects only the fixed application protocol.
            socket = this._authProtocol
                ? new WebSocket(this._url, [WS_APPLICATION_PROTOCOL, this._authProtocol])
                : new WebSocket(this._url, [WS_APPLICATION_PROTOCOL]);
            this._ws = socket;
        } catch (e) {
            // Constructor failures echo the full URL (token included) in their
            // message; log a sanitized reason instead of the error itself.
            console.error('[Responses Proxy WS] Failed to create WebSocket for %s (%s)',
                redactWsUrl(this._url), e instanceof Error ? e.name : typeof e);
            this._scheduleReconnect();
            return;
        }

        socket.onopen = () => {
            if (this._ws !== socket) {
                socket.close();
                return;
            }
            console.log('[Responses Proxy WS] Connected');
            this._connected = true;
            this._reconnectDelay = 1000;
            if (this._sessionId) {
                this.send({ type: 'subscribe', session_id: this._sessionId });
            }
            this._emit('connected', {});
        };

        socket.onmessage = (event) => {
            if (this._ws !== socket) return;
            // Guard against unreasonably large messages (10 MiB)
            if (typeof event.data === 'string' && event.data.length > 10 * 1024 * 1024) {
                console.warn('[Responses Proxy WS] Dropping oversized message (%d bytes)', event.data.length);
                return;
            }
            try {
                const data = JSON.parse(event.data);
                const msgType = data.type;

                if (msgType === 'connected') {
                    this._connectionId = data.connection_id;
                    return;
                }

                if (msgType === 'subscribed') {
                    this._emit('subscribed', data);
                    this._emit('server_requests', data.server_requests || []);
                    this._emit('tool_calls', data.tool_calls || []);
                    if (data.session_info) {
                        this._emit('session_info', data.session_info);
                    }
                    return;
                }

                if (msgType === 'server_request' || msgType === 'server_request_cancel' || msgType === 'server_request_resolved' || msgType === 'server_request_error' || msgType === 'server_request_clarify_lock_ack') {
                    this._emit(msgType, data);
                    return;
                }

                if (msgType === 'tool_call_added' || msgType === 'tool_call_updated' || msgType === 'tool_call_completed') {
                    this._emit(msgType, data);
                    this._emit('tool_call_event', data);
                    return;
                }

                if (msgType === 'tool_calls_cleared') {
                    this._emit('tool_calls_cleared', data);
                    return;
                }

                if (msgType === 'session_deleted') {
                    this._emit('session_deleted', data);
                    return;
                }

                if (msgType === 'session_info') {
                    this._emit('session_info', data);
                    return;
                }

                if (msgType === 'agent_status') {
                    this._emit('agent_status', data);
                    return;
                }

                if (msgType === 'model_options') {
                    this._emit('model_options', data);
                    return;
                }

                if (msgType === 'model_changed') {
                    this._emit('model_changed', data);
                    return;
                }

                if (msgType === 'error') {
                    this._emit('server_error', data);
                    return;
                }

                if (msgType === 'pong') {
                    return;
                }

                this._emit(msgType, data);
            } catch (e) {
                console.warn('[Responses Proxy WS] Failed to parse message:', e);
            }
        };

        socket.onclose = (event) => {
            // React StrictMode can disconnect and reconnect before the old
            // socket's close event arrives. Never let that stale event start a
            // second reconnect loop.
            if (this._ws !== socket) return;
            console.log('[Responses Proxy WS] Closed:', event.code, event.reason);
            this._connected = false;
            this._emit('disconnected', { code: event.code, reason: event.reason });
            if (!this._intentionalClose) {
                this._scheduleReconnect();
            }
        };

        socket.onerror = (event) => {
            if (this._ws !== socket) return;
            // Error events expose the full URL (token included) via
            // event.target; emit the Event object and log a redacted label.
            console.error('[Responses Proxy WS] WebSocket error on %s', redactWsUrl(this._url));
            this._emit('error', { event, url: redactWsUrl(this._url) });
        };
    }

    _scheduleReconnect() {
        this._clearReconnectTimer();
        const delay = this._reconnectDelay;
        console.log(`[Responses Proxy WS] Reconnecting in ${delay}ms...`);
        this._reconnectTimer = setTimeout(() => {
            this._reconnectDelay = Math.min(this._reconnectDelay * 2, this._maxReconnectDelay);
            this._doConnect();
        }, delay);
    }

    _clearReconnectTimer() {
        if (this._reconnectTimer) {
            clearTimeout(this._reconnectTimer);
            this._reconnectTimer = null;
        }
    }

    /**
     * @param {string} event
     * @param {any} data
     */
    _emit(event, data) {
        const callbacks = this._listeners.get(event);
        if (callbacks) {
            for (const cb of callbacks) {
                try {
                    cb(data);
                } catch (e) {
                    console.error(`[Responses Proxy WS] Listener error for "${event}":`, e);
                }
            }
        }
    }
}

// Singleton
const wsService = new WebSocketService();
export default wsService;
