import React, { useState, useEffect, useCallback } from 'react';
import { useWorkspaceFocus } from './WorkspaceContext';
import { proxyFetch, withProxyAuthToken } from '../ProxyHttp';

function formatFileSize(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value)) return '';
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function workspaceUrl(baseUrl, sessionId, endpoint, path = '') {
    if (!baseUrl || !sessionId) return '';
    const url = new URL(`${baseUrl}/v1/session/${encodeURIComponent(sessionId)}/workspace/${endpoint}`);
    if (path) url.searchParams.set('path', path);
    return url.toString();
}

function cleanWorkspacePath(path) {
    return String(path || '')
        .trim()
        .replace(/^['"]|['"]$/g, '')
        .replace(/\\/g, '/');
}

function normalizeWorkspacePath(path, root = '') {
    let normalized = cleanWorkspacePath(path);
    const normalizedRoot = cleanWorkspacePath(root).replace(/\/+$/g, '');

    if (normalizedRoot && (normalized === normalizedRoot || normalized.startsWith(`${normalizedRoot}/`))) {
        normalized = normalized.slice(normalizedRoot.length);
    }

    return normalized.replace(/^\/+/g, '').replace(/\/+/g, '/');
}

function pathBasename(path) {
    return normalizeWorkspacePath(path).split('/').filter(Boolean).pop() || String(path || '');
}

function parentPaths(path) {
    const parts = normalizeWorkspacePath(path).split('/').filter(Boolean);
    return parts.slice(0, -1).map((_, idx) => parts.slice(0, idx + 1).join('/'));
}

function addParentPaths(expanded, path) {
    const next = new Set(expanded);
    for (const parent of parentPaths(path)) {
        next.add(parent);
    }
    return next;
}

function pathsMatch(entryPath, targetPath) {
    const entry = normalizeWorkspacePath(entryPath);
    const target = normalizeWorkspacePath(targetPath);
    if (!entry || !target) return false;
    return entry === target || target.endsWith(`/${entry}`) || entry.endsWith(`/${target}`);
}

const WorkspaceEntry = React.memo(function WorkspaceEntry({
    entry,
    depth = 0,
    expanded,
    onToggle,
    onSelect,
    onSelectDirectory,
    activePath,
    selectedDirectoryPath,
}) {
    const isDirectory = entry.type === 'directory';
    const isExpanded = expanded.has(entry.path);
    const isActive = activePath === entry.path;
    const isSelectedDirectory = isDirectory && selectedDirectoryPath === entry.path;
    const childEntries = Array.isArray(entry.children) ? entry.children : [];
    const icon = isDirectory
        ? (isExpanded ? 'fa-solid fa-folder-open' : 'fa-solid fa-folder')
        : (entry.mime?.startsWith('image/') ? 'fa-solid fa-image' : entry.name?.match(/\.(md|txt|json|py|js|ts|tsx|jsx|css|html|sh|yaml|yml)$/i) ? 'fa-solid fa-file-code' : 'fa-solid fa-file');

    return (
        <>
            <div
                className={`rp-workspace-entry-row ${isDirectory ? 'directory' : 'file'} ${isActive || isSelectedDirectory ? 'active' : ''}`}
                style={{ paddingLeft: `${8 + depth * 14}px` }}
            >
                {isDirectory && (
                    <button
                        className="rp-workspace-expand-btn"
                        onClick={() => onToggle(entry.path)}
                        title={isExpanded ? 'Collapse folder' : 'Expand folder'}
                        aria-label={isExpanded ? `Collapse ${entry.name}` : `Expand ${entry.name}`}
                    >
                        <i className={`fa-solid fa-chevron-${isExpanded ? 'down' : 'right'}`}></i>
                    </button>
                )}
                <button
                    className="rp-workspace-entry"
                    onClick={() => isDirectory ? onSelectDirectory(entry.path) : onSelect(entry)}
                    title={isDirectory ? `Select ${entry.path || entry.name} as session workspace` : (entry.path || entry.name)}
                >
                    <i className={icon}></i>
                    <span className="rp-workspace-entry-name">{entry.name}</span>
                    {isDirectory && isSelectedDirectory && <i className="fa-solid fa-check rp-workspace-selected-icon" title="Selected for locking"></i>}
                    {!isDirectory && entry.size !== undefined && entry.size !== null && (
                        <span className="rp-workspace-entry-size">{formatFileSize(entry.size)}</span>
                    )}
                </button>
            </div>
            {isDirectory && isExpanded && childEntries.map(child => (
                <WorkspaceEntry
                    key={child.path || child.name}
                    entry={child}
                    depth={depth + 1}
                    expanded={expanded}
                    onToggle={onToggle}
                    onSelect={onSelect}
                    onSelectDirectory={onSelectDirectory}
                    activePath={activePath}
                    selectedDirectoryPath={selectedDirectoryPath}
                />
            ))}
        </>
    );
});

function lockedWorkspaceForChat(context, sessionId) {
    const value = context.extensionSettings?.responsesProxy?.workspaceByChat?.[sessionId];
    return typeof value === 'string' ? normalizeWorkspacePath(value) : '';
}

function updateLockedWorkspace(context, sessionId, path) {
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
}

const WorkspaceExplorer = React.memo(function WorkspaceExplorer({ baseUrl, sessionId, context }) {
    const { focusedPath, clearFocus } = useWorkspaceFocus();
    const [tree, setTree] = useState(null);
    const [expanded, setExpanded] = useState(() => new Set(['']));
    const [activeFile, setActiveFile] = useState(null);
    const [preview, setPreview] = useState(null);
    const [loading, setLoading] = useState(false);
    const [previewLoading, setPreviewLoading] = useState(false);
    const [error, setError] = useState('');
    const [selectedDirectory, setSelectedDirectory] = useState(() => lockedWorkspaceForChat(context, sessionId));
    const [lockedDirectory, setLockedDirectory] = useState(() => lockedWorkspaceForChat(context, sessionId));

    const fetchTree = useCallback(async () => {
        if (!baseUrl || !sessionId) return;
        setLoading(true);
        setError('');
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 5000);
        try {
            const url = new URL(workspaceUrl(baseUrl, sessionId, 'tree'));
            url.searchParams.set('depth', '3');
            const response = await proxyFetch(context, url.toString(), { signal: ctrl.signal });
            clearTimeout(timer);
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                throw new Error(data.detail || data.error?.message || `Workspace unavailable (${response.status})`);
            }
            setTree(data);
            setExpanded(prev => prev.size ? prev : new Set(['']));
        } catch (err) {
            clearTimeout(timer);
            if (err.name === 'AbortError') {
                setTree(null);
                setError('Workspace request timed out');
            } else {
                setTree(null);
                setError(err.message || 'Workspace unavailable');
            }
        } finally {
            setLoading(false);
        }
    }, [baseUrl, context, sessionId]);

    useEffect(() => {
        const locked = lockedWorkspaceForChat(context, sessionId);
        setActiveFile(null);
        setPreview(null);
        setExpanded(new Set(['']));
        setSelectedDirectory(locked);
        setLockedDirectory(locked);
        fetchTree();
    }, [context, sessionId, fetchTree]);

    const selectFile = useCallback(async (entry) => {
        if (!baseUrl || !sessionId || !entry?.path) return false;
        setActiveFile(entry);
        setPreview(null);
        setPreviewLoading(true);
        setError('');
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 5000);
        try {
            const response = await proxyFetch(context, workspaceUrl(baseUrl, sessionId, 'file', entry.path), { signal: ctrl.signal });
            clearTimeout(timer);
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
                throw new Error(data.detail || data.error?.message || `Preview failed (${response.status})`);
            }
            setPreview(data);
            if (data?.path) {
                const entryData = { ...data };
                delete entryData.content;
                delete entryData.kind;
                delete entryData.truncated;
                delete entryData.max_bytes;
                setActiveFile(prev => ({ ...prev, ...entryData }));
            }
            return true;
        } catch (err) {
            clearTimeout(timer);
            if (err.name === 'AbortError') {
                setError('Preview request timed out');
            } else {
                setError(err.message || 'Preview failed');
            }
            return false;
        } finally {
            setPreviewLoading(false);
        }
    }, [baseUrl, context, sessionId]);

    useEffect(() => {
        if (!focusedPath || !tree || !tree.entries) return;

        const targetPath = normalizeWorkspacePath(focusedPath, tree.root);
        const rawPath = cleanWorkspacePath(focusedPath);
        const newExpanded = addParentPaths(new Set(expanded), targetPath);
        let foundEntry = null;

        const findAndExpand = (node) => {
            if (pathsMatch(node.path, targetPath)) return node;
            if (node.type === 'directory' && node.children) {
                for (const child of node.children) {
                    const result = findAndExpand(child);
                    if (result) {
                        newExpanded.add(node.path);
                        return result;
                    }
                }
            }
            return null;
        };

        for (const entry of tree.entries) {
            const result = findAndExpand(entry);
            if (result) {
                foundEntry = result;
                break;
            }
        }

        if (foundEntry) {
            setExpanded(newExpanded);
            if (foundEntry.type !== 'directory') selectFile(foundEntry);
        } else {
            const fallbackPaths = rawPath.startsWith('/') ? [rawPath, targetPath] : [targetPath, rawPath];
            const uniqueFallbackPaths = [...new Set(fallbackPaths.filter(Boolean))];
            if (uniqueFallbackPaths.length) {
                setExpanded(newExpanded);
                (async () => {
                    for (const fallbackPath of uniqueFallbackPaths) {
                        const found = await selectFile({ name: pathBasename(fallbackPath), path: fallbackPath, type: 'file' });
                        if (found) break;
                    }
                })();
            }
        }

        clearFocus();
    }, [focusedPath, tree, expanded, selectFile, clearFocus]);

    const toggleDirectory = useCallback((path) => {
        setExpanded(prev => {
            const next = new Set(prev);
            if (next.has(path)) next.delete(path);
            else next.add(path);
            return next;
        });
    }, []);

    const lockSelection = useCallback(() => {
        updateLockedWorkspace(context, sessionId, selectedDirectory);
        setLockedDirectory(selectedDirectory);
    }, [context, sessionId, selectedDirectory]);

    const useProxyDefault = useCallback(() => {
        setSelectedDirectory('');
        updateLockedWorkspace(context, sessionId, '');
        setLockedDirectory('');
    }, [context, sessionId]);

    const downloadUrl = activeFile?.path ? withProxyAuthToken(context, workspaceUrl(baseUrl, sessionId, 'download', activeFile.path)) : '';
    const rootName = tree?.root || 'Workspace';
    const selectionLabel = selectedDirectory ? `/${selectedDirectory}` : 'Proxy default workspace';
    const lockedLabel = lockedDirectory ? `/${lockedDirectory}` : 'Proxy default workspace';
    const hasPendingSelection = selectedDirectory !== lockedDirectory;

    return (
        <div className="rp-workspace-container">
            <div className="rp-workspace-toolbar">
                <div className="rp-workspace-root" title={rootName}>
                    <i className="fa-solid fa-folder-tree"></i>
                    <span>{rootName}</span>
                </div>
                <button className="rp-icon-btn" onClick={fetchTree} title="Refresh workspace">
                    <i className={`fa-solid fa-rotate-right ${loading ? 'fa-spin' : ''}`}></i>
                </button>
            </div>

            <div className="rp-workspace-lock">
                <div className="rp-workspace-lock__status">
                    <i className="fa-solid fa-lock"></i>
                    <span title={lockedLabel}>Locked: {lockedLabel}</span>
                </div>
                <div className="rp-workspace-lock__selection" title={selectionLabel}>
                    Selected: {selectionLabel}
                </div>
                <div className="rp-workspace-lock__actions">
                    <button className="rp-workspace-lock__button" onClick={lockSelection} disabled={!hasPendingSelection}>
                        <i className="fa-solid fa-lock"></i>
                        Lock for next session
                    </button>
                    <button className="rp-workspace-lock__default" onClick={useProxyDefault} disabled={!lockedDirectory && !selectedDirectory}>
                        Use default
                    </button>
                </div>
                <div className="rp-workspace-lock__hint">
                    Click a folder to select it; use the chevron to browse. The lock is sent only when Hermes creates or rebuilds this chat session.
                </div>
            </div>

            {error && (
                <div className="rp-workspace-error">
                    <i className="fa-solid fa-triangle-exclamation"></i>
                    <span>{error}</span>
                </div>
            )}

            <div className="rp-workspace-main">
                <div className="rp-workspace-tree">
                    {loading && !tree ? (
                        <div className="rp-empty-state">
                            <i className="fa-solid fa-spinner fa-spin" style={{ color: '#475569' }}></i>
                            <div style={{ fontWeight: 500, color: '#475569' }}>Loading workspace</div>
                        </div>
                    ) : tree?.entries?.length ? (
                        tree.entries.map(entry => (
                            <WorkspaceEntry
                                key={entry.path || entry.name}
                                entry={entry}
                                expanded={expanded}
                                onToggle={toggleDirectory}
                                onSelect={selectFile}
                                onSelectDirectory={setSelectedDirectory}
                                activePath={activeFile?.path || ''}
                                selectedDirectoryPath={selectedDirectory}
                            />
                        ))
                    ) : (
                        <div className="rp-empty-state">
                            <i className="fa-solid fa-folder-open" style={{ color: '#475569' }}></i>
                            <div style={{ fontWeight: 500, color: '#475569' }}>No workspace files</div>
                        </div>
                    )}
                </div>

                <div className="rp-workspace-preview">
                    {!activeFile ? (
                        <div className="rp-empty-state">
                            <i className="fa-solid fa-file-lines" style={{ color: '#475569' }}></i>
                            <div style={{ fontWeight: 500, color: '#475569' }}>Select a file</div>
                        </div>
                    ) : (
                        <>
                            <div className="rp-workspace-preview-header">
                                <span title={activeFile.path}>{activeFile.name}</span>
                                {downloadUrl && (
                                    <a className="rp-icon-btn" href={downloadUrl} download title="Download file">
                                        <i className="fa-solid fa-download"></i>
                                    </a>
                                )}
                            </div>
                            {previewLoading ? (
                                <div className="rp-empty-state"><i className="fa-solid fa-spinner fa-spin" style={{ color: '#475569' }}></i></div>
                            ) : preview?.kind === 'image' ? (
                                <div className="rp-workspace-image-wrap"><img src={downloadUrl} alt={activeFile.name} /></div>
                            ) : preview?.kind === 'text' ? (
                                <pre className="rp-workspace-text-preview">{preview.truncated ? `Preview truncated to ${formatFileSize(preview.max_bytes)}.\n\n` : ''}{preview.content}</pre>
                            ) : (
                                <div className="rp-empty-state">
                                    <i className="fa-solid fa-file-arrow-down" style={{ color: '#475569' }}></i>
                                    <div style={{ fontWeight: 500, color: '#475569' }}>Download to view</div>
                                </div>
                            )}
                        </>
                    )}
                </div>
            </div>
        </div>
    );
});

export { WorkspaceExplorer, WorkspaceEntry, workspaceUrl, formatFileSize, normalizeWorkspacePath };
