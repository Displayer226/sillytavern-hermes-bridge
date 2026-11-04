/** Translate the proxy's shared Hermes connection facts into display states. */
export function presentHermesAuth(status) {
    if (status === null) return { kind: 'initializing', action: null };
    if (!status || typeof status !== 'object' || Array.isArray(status)) return { kind: 'unavailable', action: null };
    const flags = ['connected', 'ready', 'login_required', 'session_expired', 'web_login_enabled'];
    if (flags.some(key => typeof status[key] !== 'boolean')
        || !(status.retry_at === null || (typeof status.retry_at === 'number' && Number.isFinite(status.retry_at)
            && Number.isFinite(new Date(status.retry_at * 1000).getTime())))
        || (status.ready && !status.connected)) {
        return { kind: 'unavailable', action: null };
    }
    if (status.connected && status.ready) return { kind: 'ready', action: null };
    if (status.connected) return { kind: 'gateway_waiting', action: null };
    if (status.login_required) {
        return { kind: status.session_expired ? 'expired' : 'login_required',
            action: status.web_login_enabled ? (status.session_expired ? 'reconnect' : 'connect') : null };
    }
    if (status.retry_at !== null) return { kind: 'retrying', action: null, retryAt: status.retry_at };
    return { kind: 'initializing', action: null };
}
