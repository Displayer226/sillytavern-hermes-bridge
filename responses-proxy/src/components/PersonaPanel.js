import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import wsService from '../WebSocketService';

/* global SillyTavern, $ */

const DEFAULT_MAX_PERSONA_VERSIONS = 30;
const PERSONA_TOOL_NAME = 'sillytavern_update_persona_description';

function getLiveContext(fallbackContext) {
    try {
        if (typeof SillyTavern?.getContext === 'function') {
            return SillyTavern.getContext();
        }
    } catch (error) {
        console.warn('[Responses Proxy] Failed to refresh SillyTavern context:', error);
    }
    return fallbackContext;
}

function normalizePersonaSettings(settings = {}) {
    const persona = settings.persona && typeof settings.persona === 'object' ? settings.persona : {};
    return {
        maxVersions: Number.isFinite(Number(persona.maxVersions))
            ? Math.max(1, Math.min(100, Number(persona.maxVersions)))
            : DEFAULT_MAX_PERSONA_VERSIONS,
        autoApplyAgentPatches: persona.autoApplyAgentPatches === true,
        historyByAvatar: persona.historyByAvatar && typeof persona.historyByAvatar === 'object'
            ? persona.historyByAvatar
            : {},
    };
}

function getActiveCharacter(fallbackContext) {
    const liveContext = getLiveContext(fallbackContext);
    const characterId = liveContext?.characterId;
    const character = characterId !== undefined && characterId !== null
        ? liveContext?.characters?.[characterId]
        : null;
    return { liveContext, characterId, character };
}

function personaKey(character) {
    return String(character?.avatar || character?.name || 'active-character');
}

function shortVersionLabel(version, index) {
    const date = version?.created_at ? new Date(version.created_at) : null;
    const time = date && !Number.isNaN(date.getTime())
        ? date.toLocaleString([], { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' })
        : `Version ${index + 1}`;
    const source = version?.source ? ` · ${version.source}` : '';
    const summary = version?.summary ? ` · ${version.summary}` : '';
    return `${time}${source}${summary}`.slice(0, 140);
}

function newVersionRecord(character, content, metadata = {}) {
    return {
        id: `persona-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        created_at: new Date().toISOString(),
        avatar: character?.avatar || '',
        character_name: character?.name || '',
        source: metadata.source || 'manual',
        reason: metadata.reason || '',
        summary: metadata.summary || '',
        chat_id: metadata.chatId || '',
        session_id: metadata.sessionId || '',
        content: String(content || ''),
    };
}

function savePersonaSettings(liveContext, nextPersonaSettings) {
    if (!liveContext.extensionSettings) {
        liveContext.extensionSettings = {};
    }
    const current = liveContext.extensionSettings?.responsesProxy || {};
    liveContext.extensionSettings.responsesProxy = {
        ...current,
        persona: nextPersonaSettings,
    };
    if (typeof liveContext.saveSettingsDebounced === 'function') {
        liveContext.saveSettingsDebounced();
    }
    window.dispatchEvent(new CustomEvent('responses-proxy-settings-updated', {
        detail: liveContext.extensionSettings.responsesProxy,
    }));
}

function normalizePersonaOperation(value, args = {}) {
    const raw = String(value || '').trim().toLowerCase();
    if (['append', 'prepend', 'replace'].includes(raw)) {
        return raw;
    }
    if (raw === 'auto' || !raw) {
        if (args.append === true) return 'append';
        if (args.prepend === true) return 'prepend';
        if (args.replace === true) return 'replace';
        return 'auto';
    }
    if (['add', 'insert', 'extend'].includes(raw)) {
        return 'append';
    }
    if (['rewrite', 'overwrite', 'full_replace', 'full-replace'].includes(raw)) {
        return 'replace';
    }
    return 'auto';
}

function looksLikeFullPersona(content) {
    const text = String(content || '').trim();
    if (!text) return false;
    const sectionCount = (text.match(/^##\s+/gm) || []).length;
    const bulletCount = (text.match(/^\s*[-*]\s+/gm) || []).length;
    return /^#\s+\S/m.test(text) && (sectionCount >= 2 || bulletCount >= 4);
}

function shouldInferAppend(previous, content) {
    const current = String(previous || '').trim();
    const addition = String(content || '').trim();
    if (!current || !addition) return false;
    if (looksLikeFullPersona(addition)) return false;
    if (addition.length <= 500) return true;
    return current.length >= 1200 && addition.length < current.length * 0.45;
}

function appendPersonaText(previous, addition) {
    const current = String(previous || '').trimEnd();
    const text = String(addition || '').trim();
    if (!current) return text;
    return `${current}\n\n${text}`;
}

function prependPersonaText(previous, addition) {
    const current = String(previous || '').trimStart();
    const text = String(addition || '').trim();
    if (!current) return text;
    return `${text}\n\n${current}`;
}

function resolvePersonaToolUpdate(previous, args = {}) {
    const rawContent = String(args.description || args.content || args.text || '').trim();
    if (!rawContent) {
        throw new Error('description, content, or text is required');
    }

    let operation = normalizePersonaOperation(args.operation || args.mode, args);
    const inferredOperation = operation === 'auto';
    if (operation === 'auto') {
        operation = shouldInferAppend(previous, rawContent) ? 'append' : 'replace';
    }

    if (operation === 'append') {
        return {
            content: appendPersonaText(previous, rawContent),
            operation,
            suppliedContent: rawContent,
            inferredOperation,
        };
    }
    if (operation === 'prepend') {
        return {
            content: prependPersonaText(previous, rawContent),
            operation,
            suppliedContent: rawContent,
            inferredOperation,
        };
    }

    const previousText = String(previous || '').trim();
    const riskyShrink = previousText.length >= 1200
        && rawContent.length < previousText.length * 0.6
        && !looksLikeFullPersona(rawContent);
    if (riskyShrink && args.confirm_replace !== true && args.allow_shrink !== true) {
        throw new Error('Refusing risky persona replacement: use operation="append" for additions, or set confirm_replace=true for an intentional shorter full persona.');
    }

    return {
        content: rawContent,
        operation: 'replace',
        suppliedContent: rawContent,
        inferredOperation,
    };
}

function appendPersonaVersion(liveContext, character, content, metadata = {}) {
    const settings = liveContext.extensionSettings?.responsesProxy || {};
    const persona = normalizePersonaSettings(settings);
    const key = personaKey(character);
    const existing = Array.isArray(persona.historyByAvatar[key])
        ? persona.historyByAvatar[key]
        : [];
    const normalizedContent = String(content || '');
    const last = existing[existing.length - 1];
    if (last?.content === normalizedContent && metadata.allowDuplicate !== true) {
        return last;
    }

    const nextVersion = newVersionRecord(character, normalizedContent, {
        ...metadata,
        chatId: typeof liveContext.getCurrentChatId === 'function'
            ? liveContext.getCurrentChatId()
            : liveContext.chatId,
    });
    const nextHistory = [...existing, nextVersion].slice(-persona.maxVersions);
    const nextPersona = {
        ...persona,
        historyByAvatar: {
            ...persona.historyByAvatar,
            [key]: nextHistory,
        },
    };
    savePersonaSettings(liveContext, nextPersona);
    return nextVersion;
}

async function emitCharacterEdited(liveContext, characterId, character) {
    const eventSource = liveContext?.eventSource;
    const eventType = liveContext?.eventTypes?.CHARACTER_EDITED;
    if (!eventSource || !eventType || typeof eventSource.emit !== 'function') {
        return;
    }
    await eventSource.emit(eventType, { detail: { id: characterId, character } });
}

function updateCharacterJsonData(character, description) {
    if (!character?.json_data) {
        return;
    }
    try {
        const jsonData = JSON.parse(character.json_data);
        jsonData.description = description;
        if (jsonData.data && typeof jsonData.data === 'object') {
            jsonData.data.description = description;
        }
        character.json_data = JSON.stringify(jsonData);
        $('#character_json_data').val(character.json_data);
    } catch (error) {
        console.warn('[Responses Proxy] Failed to sync character json_data after persona update:', error);
    }
}

async function persistCharacterDescription(liveContext, characterId, character, description) {
    if (!character?.avatar) {
        throw new Error('No active character avatar found');
    }
    const response = await fetch('/api/characters/merge-attributes', {
        method: 'POST',
        headers: liveContext.getRequestHeaders(),
        body: JSON.stringify({
            avatar: character.avatar,
            description,
            data: {
                description,
            },
        }),
    });
    if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.message || `SillyTavern returned HTTP ${response.status}`);
    }

    character.description = description;
    character.data = {
        ...(character.data || {}),
        description,
    };
    updateCharacterJsonData(character, description);

    if (String(liveContext.characterId) === String(characterId)) {
        $('#description_textarea').val(description).trigger('input');
    }
    await emitCharacterEdited(liveContext, characterId, character);
}

function sendFrontendToolResult(requestId, payload) {
    if (!requestId) return;
    wsService.send({
        type: 'frontend_tool_result',
        request_id: requestId,
        ...payload,
    });
}

function PersonaPanel({ context, chatId, isOpen, onOpenChange }) {
    const [revision, setRevision] = useState(0);
    const [draft, setDraft] = useState('');
    const [draftDirty, setDraftDirty] = useState(false);
    const [selectedVersionId, setSelectedVersionId] = useState('');
    const [pendingPatch, setPendingPatch] = useState(null);
    const [busy, setBusy] = useState(false);
    const [message, setMessage] = useState(null);
    const chatIdRef = useRef(chatId);
    chatIdRef.current = chatId;

    const active = useMemo(() => getActiveCharacter(context), [context, revision]);
    const currentDescription = String(active.character?.description || '');
    const settings = active.liveContext?.extensionSettings?.responsesProxy || {};
    const personaSettings = normalizePersonaSettings(settings);
    const history = useMemo(() => {
        if (!active.character) return [];
        const key = personaKey(active.character);
        return Array.isArray(personaSettings.historyByAvatar[key])
            ? personaSettings.historyByAvatar[key]
            : [];
    }, [active.character, personaSettings.historyByAvatar]);

    const selectedVersion = useMemo(
        () => history.find(version => version.id === selectedVersionId) || history[history.length - 1] || null,
        [history, selectedVersionId],
    );

    useEffect(() => {
        if (!draftDirty) {
            setDraft(currentDescription);
        }
    }, [currentDescription, draftDirty]);

    useEffect(() => {
        if (history.length > 0 && !history.some(version => version.id === selectedVersionId)) {
            setSelectedVersionId(history[history.length - 1].id);
        } else if (history.length === 0 && selectedVersionId) {
            setSelectedVersionId('');
        }
    }, [history, selectedVersionId]);

    useEffect(() => {
        if (!message) return undefined;
        const timer = setTimeout(() => setMessage(null), 6000);
        return () => clearTimeout(timer);
    }, [message]);

    const refresh = useCallback(() => setRevision(value => value + 1), []);

    useEffect(() => {
        const liveContext = getLiveContext(context);
        const eventSource = liveContext?.eventSource;
        if (!eventSource) return undefined;
        const events = [
            liveContext.eventTypes?.CHAT_CHANGED,
            liveContext.eventTypes?.CHAT_LOADED,
            liveContext.eventTypes?.CHARACTER_EDITED,
        ].filter(Boolean);
        events.forEach(eventType => eventSource.on(eventType, refresh));
        return () => {
            events.forEach(eventType => eventSource.removeListener(eventType, refresh));
        };
    }, [context, refresh]);

    const snapshotCurrent = useCallback((metadata = {}) => {
        const { liveContext, character } = getActiveCharacter(context);
        if (!character) {
            throw new Error('No active character selected');
        }
        return appendPersonaVersion(liveContext, character, character.description || '', metadata);
    }, [context]);

    const applyDescription = useCallback(async (nextDescription, metadata = {}) => {
        const { liveContext, characterId, character } = getActiveCharacter(context);
        if (!character) {
            throw new Error('No active character selected');
        }

        const normalized = String(nextDescription || '');
        const previous = String(character.description || '');
        if (normalized.trim() === '') {
            throw new Error('Persona description cannot be empty');
        }
        if (normalized === previous) {
            setMessage({ tone: 'ok', text: 'Persona is already up to date' });
            return {
                status: 'unchanged',
                character: {
                    name: character.name || '',
                    avatar: character.avatar || '',
                },
            };
        }

        appendPersonaVersion(liveContext, character, previous, {
            source: 'snapshot',
            summary: 'Before persona update',
            sessionId: chatIdRef.current,
        });
        await persistCharacterDescription(liveContext, characterId, character, normalized);
        const version = appendPersonaVersion(liveContext, character, normalized, {
            source: metadata.source || 'manual',
            reason: metadata.reason || '',
            summary: metadata.summary || 'Updated persona description',
            sessionId: chatIdRef.current,
            allowDuplicate: metadata.allowDuplicate,
        });
        setSelectedVersionId(version?.id || '');
        setDraft(normalized);
        setDraftDirty(false);
        refresh();
        setMessage({ tone: 'ok', text: 'Persona updated and snapshotted' });
        return {
            status: 'applied',
            character: {
                name: character.name || '',
                avatar: character.avatar || '',
            },
            version,
        };
    }, [context, refresh]);

    useEffect(() => {
        const unsubscribe = wsService.on('persona_patch_request', async (data) => {
            if (data.session_id && data.session_id !== chatIdRef.current) return;
            let resolved;
            try {
                resolved = resolvePersonaToolUpdate(currentDescription, {
                    description: data.content || data.description || data.persona || '',
                    operation: data.operation || data.mode,
                    append: data.append,
                    prepend: data.prepend,
                    replace: data.replace,
                    confirm_replace: data.confirm_replace,
                    allow_shrink: data.allow_shrink,
                });
            } catch (error) {
                console.warn('[Responses Proxy] Ignoring invalid persona patch request:', data, error);
                setMessage({ tone: 'error', text: error?.message || 'Invalid persona patch request' });
                return;
            }

            const patch = {
                id: data.request_id || `persona-patch-${Date.now()}`,
                content: resolved.content,
                summary: data.summary || 'Agent proposed a persona update',
                reason: data.reason || '',
                source: data.source || 'agent',
                operation: resolved.operation,
                suppliedContent: resolved.suppliedContent,
                inferredOperation: resolved.inferredOperation,
                created_at: data.created_at || new Date().toISOString(),
            };

            if (personaSettings.autoApplyAgentPatches) {
                try {
                    setBusy(true);
                    await applyDescription(resolved.content, patch);
                    SillyTavern.toastr?.success?.('Agent persona update applied');
                } catch (error) {
                    const text = error?.message || 'Failed to apply agent persona update';
                    setMessage({ tone: 'error', text });
                    SillyTavern.toastr?.error?.(text);
                } finally {
                    setBusy(false);
                }
                return;
            }

            setPendingPatch(patch);
            onOpenChange?.(true);
            SillyTavern.toastr?.info?.('Agent proposed a persona update');
        });
        return unsubscribe;
    }, [applyDescription, currentDescription, onOpenChange, personaSettings.autoApplyAgentPatches]);

    useEffect(() => {
        const unsubscribe = wsService.on('frontend_tool_request', async (data) => {
            if (data.tool_name !== PERSONA_TOOL_NAME) return;
            if (data.session_id && data.session_id !== chatIdRef.current) return;

            const args = data.arguments && typeof data.arguments === 'object' ? data.arguments : {};
            const summary = String(args.summary || 'Agent proposed a persona update').trim();
            const reason = String(args.reason || '').trim();
            const mcpRequestId = data.request_id;
            let resolved;

            try {
                resolved = resolvePersonaToolUpdate(currentDescription, args);
            } catch (error) {
                const text = error?.message || 'Invalid persona update request';
                sendFrontendToolResult(mcpRequestId, {
                    status: 'error',
                    session_id: chatIdRef.current,
                    tool_name: PERSONA_TOOL_NAME,
                    message: text,
                });
                return;
            }

            const patch = {
                id: mcpRequestId || `persona-tool-${Date.now()}`,
                content: resolved.content,
                summary,
                reason,
                source: 'mcp',
                operation: resolved.operation,
                suppliedContent: resolved.suppliedContent,
                inferredOperation: resolved.inferredOperation,
                created_at: data.created_at || new Date().toISOString(),
                mcpRequestId,
            };

            const requiresApproval = args.require_approval === true;
            if (personaSettings.autoApplyAgentPatches && !requiresApproval) {
                try {
                    setBusy(true);
                    const result = await applyDescription(resolved.content, patch);
                    sendFrontendToolResult(mcpRequestId, {
                        status: result?.status || 'applied',
                        session_id: chatIdRef.current,
                        tool_name: PERSONA_TOOL_NAME,
                        message: result?.status === 'unchanged' ? 'Persona was already up to date' : 'Persona update applied',
                        operation: resolved.operation,
                        inferred_operation: resolved.inferredOperation,
                        result,
                        character: result?.character,
                        version: result?.version,
                    });
                    SillyTavern.toastr?.success?.('MCP persona update applied');
                } catch (error) {
                    const text = error?.message || 'Failed to apply MCP persona update';
                    setMessage({ tone: 'error', text });
                    sendFrontendToolResult(mcpRequestId, {
                        status: 'error',
                        session_id: chatIdRef.current,
                        tool_name: PERSONA_TOOL_NAME,
                        message: text,
                    });
                    SillyTavern.toastr?.error?.(text);
                } finally {
                    setBusy(false);
                }
                return;
            }

            setPendingPatch(patch);
            onOpenChange?.(true);
            SillyTavern.toastr?.info?.('Hermes requested a persona update');
        });
        return unsubscribe;
    }, [applyDescription, currentDescription, onOpenChange, personaSettings.autoApplyAgentPatches]);

    const handleSnapshot = useCallback(() => {
        try {
            const version = snapshotCurrent({ source: 'manual', summary: 'Manual snapshot', sessionId: chatIdRef.current });
            setSelectedVersionId(version?.id || '');
            refresh();
            setMessage({ tone: 'ok', text: 'Persona snapshot saved' });
        } catch (error) {
            setMessage({ tone: 'error', text: error?.message || 'Snapshot failed' });
        }
    }, [refresh, snapshotCurrent]);

    const handleApplyDraft = useCallback(async () => {
        const confirmed = window.confirm('Apply this draft to the active SillyTavern character description?');
        if (!confirmed) return;
        try {
            setBusy(true);
            await applyDescription(draft, { source: 'manual', summary: 'Manual persona edit' });
        } catch (error) {
            const text = error?.message || 'Persona update failed';
            setMessage({ tone: 'error', text });
            SillyTavern.toastr?.error?.(text);
        } finally {
            setBusy(false);
        }
    }, [applyDescription, draft]);

    const handleRestoreVersion = useCallback(async () => {
        if (!selectedVersion) return;
        const confirmed = window.confirm('Restore the selected persona version? The current description will be snapshotted first.');
        if (!confirmed) return;
        try {
            setBusy(true);
            await applyDescription(selectedVersion.content, {
                source: 'rollback',
                summary: `Rollback to ${selectedVersion.created_at || selectedVersion.id}`,
                reason: selectedVersion.reason || '',
                allowDuplicate: true,
            });
        } catch (error) {
            const text = error?.message || 'Persona rollback failed';
            setMessage({ tone: 'error', text });
            SillyTavern.toastr?.error?.(text);
        } finally {
            setBusy(false);
        }
    }, [applyDescription, selectedVersion]);

    const handleApprovePatch = useCallback(async () => {
        if (!pendingPatch) return;
        try {
            setBusy(true);
            const result = await applyDescription(pendingPatch.content, {
                source: pendingPatch.source || 'agent',
                summary: pendingPatch.operation ? `${pendingPatch.summary} (${pendingPatch.operation})` : pendingPatch.summary,
                reason: pendingPatch.reason,
            });
            sendFrontendToolResult(pendingPatch.mcpRequestId, {
                status: result?.status || 'applied',
                session_id: chatIdRef.current,
                tool_name: PERSONA_TOOL_NAME,
                message: result?.status === 'unchanged' ? 'Persona was already up to date' : 'Persona update approved and applied',
                operation: pendingPatch.operation,
                inferred_operation: pendingPatch.inferredOperation,
                result,
                character: result?.character,
                version: result?.version,
            });
            setPendingPatch(null);
            SillyTavern.toastr?.success?.('Agent persona update approved');
        } catch (error) {
            const text = error?.message || 'Failed to approve persona update';
            setMessage({ tone: 'error', text });
            sendFrontendToolResult(pendingPatch.mcpRequestId, {
                status: 'error',
                session_id: chatIdRef.current,
                tool_name: PERSONA_TOOL_NAME,
                message: text,
            });
            SillyTavern.toastr?.error?.(text);
        } finally {
            setBusy(false);
        }
    }, [applyDescription, pendingPatch]);

    const handleRejectPatch = useCallback(() => {
        sendFrontendToolResult(pendingPatch?.mcpRequestId, {
            status: 'rejected',
            session_id: chatIdRef.current,
            tool_name: PERSONA_TOOL_NAME,
            message: 'User rejected the persona update',
        });
        setPendingPatch(null);
        setMessage({ tone: 'ok', text: 'Agent persona update rejected' });
    }, [pendingPatch]);

    if (!isOpen) {
        return null;
    }

    return (
        <div className="rp-persona-panel">
            <div className="rp-persona-header">
                <div>
                    <div className="rp-persona-title">
                        <i className="fa-solid fa-id-card-clip"></i>
                        Agent Persona
                    </div>
                    <div className="rp-persona-subtitle">
                        {active.character ? `${active.character.name || 'Character'} · ${history.length} version${history.length === 1 ? '' : 's'}` : 'No active character'}
                    </div>
                </div>
                <button className="rp-icon-btn" onClick={() => onOpenChange?.(false)} title="Close persona panel">
                    <i className="fa-solid fa-chevron-up"></i>
                </button>
            </div>

            {pendingPatch && (
                <div className="rp-persona-pending">
                    <div className="rp-persona-pending-title">
                        <i className="fa-solid fa-wand-magic-sparkles"></i>
                        Agent proposed a persona update
                    </div>
                    <div className="rp-persona-pending-summary">{pendingPatch.summary}</div>
                    {pendingPatch.reason && <div className="rp-persona-pending-reason">{pendingPatch.reason}</div>}
                    <div className="rp-persona-preview">{pendingPatch.content}</div>
                    <div className="rp-persona-actions">
                        <button className="rp-agent-control-btn" onClick={handleApprovePatch} disabled={busy || !active.character}>
                            <i className={`fa-solid ${busy ? 'fa-spinner fa-spin' : 'fa-check'}`}></i>
                            <span>Approve</span>
                        </button>
                        <button className="rp-agent-control-btn danger" onClick={handleRejectPatch} disabled={busy}>
                            <i className="fa-solid fa-xmark"></i>
                            <span>Reject</span>
                        </button>
                    </div>
                </div>
            )}

            <textarea
                className="rp-persona-editor"
                value={draft}
                onChange={(event) => {
                    setDraft(event.target.value);
                    setDraftDirty(true);
                }}
                disabled={!active.character || busy}
                placeholder="Active character description"
            />

            <div className="rp-persona-actions">
                <button className="rp-agent-control-btn" onClick={() => { setDraft(currentDescription); setDraftDirty(false); }} disabled={!active.character || busy}>
                    <i className="fa-solid fa-rotate"></i>
                    <span>Load</span>
                </button>
                <button className="rp-agent-control-btn" onClick={handleSnapshot} disabled={!active.character || busy}>
                    <i className="fa-solid fa-camera"></i>
                    <span>Snapshot</span>
                </button>
                <button className="rp-agent-control-btn" onClick={handleApplyDraft} disabled={!active.character || busy || !draftDirty}>
                    <i className={`fa-solid ${busy ? 'fa-spinner fa-spin' : 'fa-floppy-disk'}`}></i>
                    <span>Apply</span>
                </button>
            </div>

            {history.length > 0 && (
                <div className="rp-persona-history">
                    <div className="rp-session-row" style={{ marginBottom: '6px' }}>
                        <span className="rp-session-label">Version History</span>
                        <span className="rp-session-value">{history.length} saved</span>
                    </div>
                    <select
                        className="rp-model-select"
                        value={selectedVersionId}
                        onChange={(event) => setSelectedVersionId(event.target.value)}
                        disabled={busy}
                    >
                        {history.map((version, index) => (
                            <option key={version.id} value={version.id}>
                                {shortVersionLabel(version, index)}
                            </option>
                        ))}
                    </select>
                    <div className="rp-persona-actions">
                        <button className="rp-agent-control-btn" onClick={() => { setDraft(selectedVersion?.content || ''); setDraftDirty(true); }} disabled={!selectedVersion || busy}>
                            <i className="fa-solid fa-file-import"></i>
                            <span>Load Version</span>
                        </button>
                        <button className="rp-agent-control-btn" onClick={handleRestoreVersion} disabled={!selectedVersion || busy || !active.character}>
                            <i className="fa-solid fa-clock-rotate-left"></i>
                            <span>Restore</span>
                        </button>
                    </div>
                </div>
            )}

            {message && (
                <div className={`rp-agent-control-feedback ${message.tone === 'error' ? 'error' : ''}`}>
                    <i className={`fa-solid ${message.tone === 'error' ? 'fa-triangle-exclamation' : 'fa-circle-check'}`}></i>
                    <span>{message.text}</span>
                </div>
            )}
        </div>
    );
}

export default PersonaPanel;
