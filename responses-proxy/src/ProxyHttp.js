import { deriveWsUrl } from './WebSocketService';

export function proxyAuthToken(context) {
    return context.extensionSettings?.responsesProxy?.wsToken?.trim() || '';
}

export function proxyAuthHeaders(context, headers = {}) {
    const next = new Headers(headers);
    const token = proxyAuthToken(context);
    if (token && !next.has('Authorization')) {
        next.set('Authorization', `Bearer ${token}`);
    }
    return next;
}

export function proxyFetch(context, url, options = {}) {
    return fetch(url, {
        ...options,
        headers: proxyAuthHeaders(context, options.headers),
    });
}

export function proxyHttpBaseUrl(context) {
    const extSettings = context.extensionSettings?.responsesProxy || {};
    const wsUrl = extSettings.wsUrl?.trim() || deriveWsUrl(context);
    try {
        const url = new URL(wsUrl);
        url.protocol = url.protocol === 'wss:' ? 'https:' : 'http:';
        url.pathname = url.pathname.replace(/\/ws\/?$/, '').replace(/\/$/, '');
        url.search = '';
        url.hash = '';
        return url.toString().replace(/\/$/, '');
    } catch (e) {
        return '';
    }
}

export function withProxyAuthToken(context, url) {
    const token = proxyAuthToken(context);
    if (!token || !url) return url;
    try {
        const parsed = new URL(url);
        parsed.searchParams.set('token', token);
        return parsed.toString();
    } catch (e) {
        return url;
    }
}
