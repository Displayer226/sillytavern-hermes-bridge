import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { proxyFetch } from '../ProxyHttp';

function workspaceTreeUrl(baseUrl, sessionId) {
    if (!baseUrl || !sessionId) return '';
    const url = new URL(`${baseUrl}/v1/session/${encodeURIComponent(sessionId)}/workspace/tree`);
    url.searchParams.set('depth', '3');
    return url.toString();
}

function folderOptions(entries, prefix = '') {
    if (!Array.isArray(entries)) return [];
    return entries.flatMap((entry) => {
        if (entry?.type !== 'directory') return [];
        const path = String(entry.path || '').replace(/^\/+|\/+$/g, '');
        const label = `${prefix}${entry.name || path || 'Workspace root'}`;
        return [
            { path, label },
            ...folderOptions(entry.children, `${prefix}└ `),
        ];
    });
}

/**
 * Pick a directory under the proxy-configured WORKSPACE_ROOT for a chat.
 * The browser stores only a relative path. The proxy validates and translates it
 * to its configured host-side root before it is sent to Hermes.
 */
export function WorkspaceSelector({ baseUrl, sessionId, context }) {
    const [tree, setTree] = useState(null);
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(false);
    const settings = context.extensionSettings.responsesProxy || {};
    const selected = typeof settings.workspaceByChat?.[sessionId] === 'string'
        ? settings.workspaceByChat[sessionId]
        : '';

    const load = useCallback(async () => {
        const url = workspaceTreeUrl(baseUrl, sessionId);
        if (!url) return;
        setLoading(true);
        setError('');
        try {
            const response = await proxyFetch(context, url);
            const payload = await response.json().catch(() => ({}));
            if (!response.ok) {
                throw new Error(payload.detail || `Workspace unavailable (${response.status})`);
            }
            setTree(payload);
        } catch (err) {
            setTree(null);
            setError(err.message || 'Workspace unavailable');
        } finally {
            setLoading(false);
        }
    }, [baseUrl, context, sessionId]);

    useEffect(() => { load(); }, [load]);

    const options = useMemo(() => [
        { path: '', label: tree?.root ? `Workspace root (${tree.root})` : 'Workspace root' },
        ...folderOptions(tree?.entries),
    ], [tree]);

    const save = useCallback((path) => {
        const workspaceByChat = {
            ...(context.extensionSettings.responsesProxy?.workspaceByChat || {}),
        };
        if (path) workspaceByChat[sessionId] = path;
        else delete workspaceByChat[sessionId];
        context.extensionSettings.responsesProxy = {
            ...context.extensionSettings.responsesProxy,
            workspaceByChat,
        };
        context.saveSettingsDebounced();
        window.dispatchEvent(new CustomEvent('responses-proxy-settings-updated', {
            detail: context.extensionSettings.responsesProxy,
        }));
    }, [context, sessionId]);

    if (!sessionId) return null;

    return (
        <div className="rp-workspace-selector">
            <div className="rp-workspace-selector__title">
                <i className="fa-solid fa-folder-tree"></i>
                <span>Session workspace</span>
                <button className="rp-icon-btn" onClick={load} title="Refresh workspace folders" disabled={loading}>
                    <i className={`fa-solid fa-rotate${loading ? ' fa-spin' : ''}`}></i>
                </button>
            </div>
            <select
                className="text_pole rp-workspace-selector__select"
                value={selected}
                onChange={(event) => save(event.target.value)}
                disabled={loading || !tree}
                title="Applied when Hermes creates the next session for this chat"
            >
                {options.map((option) => (
                    <option key={option.path || '__root__'} value={option.path}>{option.label}</option>
                ))}
            </select>
            {error ? (
                <div className="rp-workspace-selector__error">{error}</div>
            ) : (
                <div className="rp-workspace-selector__hint">
                    Stored per chat; applied when Hermes creates its next session.
                </div>
            )}
        </div>
    );
}
