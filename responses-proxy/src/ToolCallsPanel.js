import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import useDraggableFloatingButton from './useDraggableFloatingButton';
import VoiceCallTrigger from './VoiceCallTrigger';
import { useWorkspaceFocus } from './components/WorkspaceContext';
import wsService from './WebSocketService';
import { TerminalConsole, isConsoleToolCall } from './components/TerminalConsole';
import { DiffsTab } from './components/DiffViewer';
import { TodoChecklist, hasTodoSnapshot } from './components/TodoChecklist';
import { WorkspaceExplorer } from './components/WorkspaceExplorer';
import { ClarifyCard, ApprovalCard, SudoPasswordCard } from './components/InteractionCards';
import {
    normalizeServerRequestSnapshot,
    buildClarifyLock,
    buildServerRequestResponse,
    removeServerRequest,
    serverRequestKey,
    classifyServerRequestError,
    upsertServerRequest,
} from './serverRequests';
import PersonaPanel from './components/PersonaPanel';
import { proxyFetch, proxyHttpBaseUrl } from './ProxyHttp';

/* global SillyTavern, $ */

/**
 * Simple throttle: ensures `fn` is called at most once every `interval` ms.
 */
function throttle(fn, interval) {
    let last = 0;
    return function (...args) {
        const now = Date.now();
        if (now - last >= interval) {
            last = now;
            fn.apply(this, args);
        }
    };
}

const TOOL_ICON_RULES = [
    { pattern: /\b(exec|terminal|shell|bash|zsh|powershell|cmd|command|run|python|node|npm|docker)\b/i, icon: 'fa-solid fa-terminal' },
    { pattern: /\b(apply_patch|patch|diff|edit|write|update_file|replace|delete_file)\b/i, icon: 'fa-solid fa-file-pen' },
    { pattern: /\b(read|fetch_file|open_file|cat|file|document)\b/i, icon: 'fa-solid fa-file-lines' },
    { pattern: /\b(search|grep|rg|find|lookup|query)\b/i, icon: 'fa-solid fa-magnifying-glass' },
    { pattern: /\b(web|browser|url|fetch|http|open_url|navigate|page)\b/i, icon: 'fa-solid fa-globe' },
    { pattern: /\b(git|github|commit|branch|pull_request|pr|issue)\b/i, icon: 'fa-solid fa-code-branch' },
    { pattern: /\b(memory|remember|recall|note|knowledge)\b/i, icon: 'fa-solid fa-brain' },
    { pattern: /\b(rag|docs?|documentation|manual|reference|collection)\b/i, icon: 'fa-solid fa-book-open' },
    { pattern: /\b(image|screenshot|vision|photo|picture|view_image)\b/i, icon: 'fa-solid fa-image' },
    { pattern: /\b(plan|todo|task|checklist|update_plan)\b/i, icon: 'fa-solid fa-list-check' },
    { pattern: /\b(spawn|delegate|agent|subagent|parallel|multi_tool)\b/i, icon: 'fa-solid fa-diagram-project' },
    { pattern: /\b(time|date|clock|schedule|calendar)\b/i, icon: 'fa-solid fa-clock' },
    { pattern: /\b(weather|forecast)\b/i, icon: 'fa-solid fa-cloud-sun' },
    { pattern: /\b(finance|stock|ticker|price|crypto)\b/i, icon: 'fa-solid fa-chart-line' },
    { pattern: /\b(database|sql|db|collection|storage)\b/i, icon: 'fa-solid fa-database' },
];

const LEFT_TABS = ['console', 'diffs', 'todos'];
const DEFAULT_NOTIFICATION_SETTINGS = {
    clarify: true,
    approval: true,
    sudo: true,
    toolError: true,
};

const AGENT_CONTROL_LABELS = {
    interrupt: 'Interrupt',
    steer: 'Steer',
    undo: 'Undo',
    retry: 'Retry',
    compress: 'Compress',
    persona: 'Persona',
};

function agentControlLabel(action) {
    return AGENT_CONTROL_LABELS[action] || String(action || 'Action');
}

function agentControlResultMessage(data) {
    if (data?.message) return data.message;
    const action = data?.action || '';
    const result = data?.result || {};
    if (action === 'steer' && result.status) return `Steer ${result.status}`;
    if (action === 'undo' && result.removed !== undefined) return `Undo removed ${result.removed} item${Number(result.removed) === 1 ? '' : 's'}`;
    if (action === 'compress' && result.session_expired && result.skipped) {
        return 'Hermes session expired; there is no live context to compress';
    }
    if (action === 'compress' && result.after_tokens !== undefined && result.before_tokens !== undefined) {
        return `Compress reduced context from ${result.before_tokens} to ${result.after_tokens} tokens`;
    }
    return `${agentControlLabel(action)} completed`;
}

const HERMES_SESSION_STATUS = {
    active: { label: 'Active', icon: 'fa-circle-check', title: 'Hermes session is live' },
    working: { label: 'Working', icon: 'fa-circle-notch fa-spin', title: 'Hermes is processing this chat' },
    rebuilding: { label: 'Rebuilding', icon: 'fa-arrows-rotate fa-spin', title: 'Hermes is rebuilding this session from SillyTavern history' },
    expired: { label: 'Expired', icon: 'fa-clock-rotate-left', title: 'Hermes session expired; the next message will rebuild it' },
    not_started: { label: 'Not started', icon: 'fa-circle', title: 'No Hermes session has been created for this chat' },
    unavailable: { label: 'Unavailable', icon: 'fa-triangle-exclamation', title: 'Hermes session status cannot be verified' },
};

function lastUserTurnRange(chat) {
    if (!Array.isArray(chat) || chat.length === 0) return null;
    for (let i = chat.length - 1; i >= 0; i--) {
        if (chat[i]?.is_user) {
            return { start: i, end: chat.length - 1, count: chat.length - i };
        }
    }
    return null;
}

async function deleteVisibleMessageRange(context, start, end) {
    const chat = Array.isArray(context.chat) ? context.chat : null;
    if (!chat || start < 0 || end < start || end >= chat.length) return 0;

    let deleted = 0;
    let needsReload = false;
    for (let index = end; index >= start; index--) {
        const beforeLength = chat.length;
        if (typeof context.deleteMessage === 'function') {
            await context.deleteMessage(index, undefined, false);
        }
        if (chat.length === beforeLength) {
            chat.splice(index, 1);
            needsReload = true;
        }
        deleted += 1;
    }

    if (typeof context.saveChat === 'function') {
        await context.saveChat();
    }
    if (needsReload && typeof context.reloadCurrentChat === 'function') {
        await context.reloadCurrentChat();
    }

    return deleted;
}

function normalizedExtensionSettings(context) {
    const settings = context.extensionSettings?.responsesProxy || {};
    return {
        ...settings,
        themeMode: settings.themeMode || 'system',
        browserNotifications: {
            ...DEFAULT_NOTIFICATION_SETTINGS,
            ...(settings.browserNotifications || {}),
        },
    };
}

let _toolCallCounter = 0;
let _agentControlRequestCounter = 0;

function toolCallKey(call) {
    if (!call) return '';
    // Use call.id if available; otherwise generate a unique key with counter
    const id = call.id ?? `${call.name || 'tool'}:${call.timestamp || ''}:${++_toolCallCounter}`;
    if (!call.id) call.id = id; // Ensure stability
    const base = String(id);
    const valueHash = (value) => {
        if (value === null || value === undefined) return '';
        const str = typeof value === 'string' ? value : JSON.stringify(value);
        let hash = 0;
        for (let i = 0; i < str.length; i++) {
            hash = ((hash << 5) - hash) + str.charCodeAt(i);
            hash |= 0;
        }
        return hash;
    };
    const revision = [
        call.status || '',
        valueHash(call.output),
        valueHash(call.result_text),
        valueHash(call.stdout),
        valueHash(call.stderr),
        valueHash(call.inline_diff),
        valueHash(call.todos),
        valueHash(call.error),
    ].join(':');
    return `${base}:${revision}`;
}

function emptyLeftReadState() {
    return LEFT_TABS.reduce((acc, tab) => {
        acc[tab] = new Set();
        return acc;
    }, {});
}

function leftTabToolCallIds(toolCalls, tab) {
    if (tab === 'console') return toolCalls.filter(isConsoleToolCall).map(toolCallKey).filter(Boolean);
    if (tab === 'diffs') return toolCalls.filter(call => call.inline_diff).map(toolCallKey).filter(Boolean);
    if (tab === 'todos') return toolCalls.filter(hasTodoSnapshot).map(toolCallKey).filter(Boolean);
    return [];
}

function unreadCount(ids, readSet) {
    const read = readSet || new Set();
    return ids.filter(id => !read.has(id)).length;
}

function isToolError(call) {
    if (!call) return false;
    const exitCode = Number(call.exit_code ?? call.returncode);
    return call.status === 'error' || Boolean(call.error) || (Number.isFinite(exitCode) && exitCode !== 0);
}

function toolIconClass(toolName) {
    const name = String(toolName || '').replace(/[_-]+/g, ' ');
    const rule = TOOL_ICON_RULES.find(item => item.pattern.test(name));
    return rule?.icon || 'fa-solid fa-gear';
}

const NotificationBadge = React.memo(({ count, className = '' }) => {
    if (!count) return null;
    return <span className={`rp-notification-badge ${className}`}>{count > 99 ? '99+' : count}</span>;
});

// ClarifyCard, ApprovalCard, and TerminalConsole are imported from ./components/.
// Diff utilities and WorkspaceExplorer are also extracted into ./components/.

function ToolCallsPanel({ context, voiceOpen, voiceActive, onOpenVoice }) {
    const { focusPath } = useWorkspaceFocus();
    const [isOpen, setIsOpen] = useState(false);
    const [isLeftOpen, setIsLeftOpen] = useState(false);
    const [isPersonaPanelOpen, setIsPersonaPanelOpen] = useState(false);
    const [activeLeftTab, setActiveLeftTab] = useState('console');
    const [toolCalls, setToolCalls] = useState([]);
    const [toolCallsPage, setToolCallsPage] = useState({ total: 0, limit: 100, offset: 0, has_more: false });
    const [toolCallsLoading, setToolCallsLoading] = useState(false);
    const [toolCallFilters, setToolCallFilters] = useState({ status: '', tool: '', q: '' });
    const [rightReadCallIds, setRightReadCallIds] = useState(() => new Set());
    const [leftReadCallIds, setLeftReadCallIds] = useState(() => emptyLeftReadState());
    const [chatId, setChatId] = useState(null);
    const [wsStatus, setWsStatus] = useState('disconnected'); // 'connected' | 'disconnected' | 'connecting'
    const [sessionInfo, setSessionInfo] = useState({});
    const [agentStatus, setAgentStatus] = useState({ active: false });
    const [streamingEstimate, setStreamingEstimate] = useState(null);
    const [modelOptions, setModelOptions] = useState([]);
    const [selectedModel, setSelectedModel] = useState(context.extensionSettings?.responsesProxy?.selectedModel || '');
    const [profileOptions, setProfileOptions] = useState([]);
    const [selectedProfile, setSelectedProfile] = useState('default');
    const [agentControlBusy, setAgentControlBusy] = useState({});
    const [agentControlMessage, setAgentControlMessage] = useState(null);
    const panelRef = useRef(null);
    const latestCallRef = useRef(null);
    const prevWsUrlRef = useRef(null);
    const agentControlWaitersRef = useRef(new Map());
    const chatIdRef = useRef(chatId);
    chatIdRef.current = chatId;
    const [serverRequests, setServerRequests] = useState([]);
    const [serverRequestResetTokens, setServerRequestResetTokens] = useState({});
    const pendingClarifies = useMemo(() => serverRequests
        .filter((item) => item.method === 'clarify')
        .map((request) => ({ ...request.params, rpc_id: request.rpc_id, request_id: request.rpc_id, method: request.method })), [serverRequests]);
    const pendingApprovals = useMemo(() => serverRequests
        .filter((item) => item.method === 'approval')
        .map((request) => ({ ...request.params, rpc_id: request.rpc_id, method: request.method })), [serverRequests]);
    const pendingSudos = useMemo(() => serverRequests
        .filter((item) => item.method === 'sudo')
        .map((request) => ({ ...request.params, rpc_id: request.rpc_id, request_id: request.rpc_id, method: request.method })), [serverRequests]);
    const [extensionSettings, setExtensionSettings] = useState(() => normalizedExtensionSettings(context));
    const extensionSettingsRef = useRef(extensionSettings);
    const notifiedToolErrorsRef = useRef(new Set());
    extensionSettingsRef.current = extensionSettings;
    const openLeftDrawer = useCallback(() => setIsLeftOpen(true), []);
    const openRightDrawer = useCallback(() => setIsOpen(true), []);
    const leftFloatingButton = useDraggableFloatingButton('responses-proxy-floating-left', openLeftDrawer);
    const rightFloatingButton = useDraggableFloatingButton('responses-proxy-floating-right', openRightDrawer);

    // Append CSS styles dynamically on mount — replace any orphaned element
    // from a previous hot-reload cycle.
    useEffect(() => {
        const styleId = 'responses-proxy-panel-styles';
        // Remove any stale element from a previous load
        const existing = document.getElementById(styleId);
        if (existing) {
            existing.remove();
        }
        const style = document.createElement('style');
        style.id = styleId;
        style.textContent = `
                .rp-theme-root {
                    --rp-text: var(--SmartThemeBodyColor, #e2e8f0);
                    --rp-muted-text: color-mix(in srgb, var(--SmartThemeBodyColor, #e2e8f0) 62%, transparent);
                    --rp-dim-text: color-mix(in srgb, var(--SmartThemeBodyColor, #e2e8f0) 42%, transparent);
                    --rp-panel-bg: color-mix(in srgb, var(--SmartThemeBlurTintColor, #121218) 94%, transparent);
                    --rp-panel-strong-bg: color-mix(in srgb, var(--SmartThemeBlurTintColor, #121218) 82%, #000000 18%);
                    --rp-card-bg: color-mix(in srgb, var(--SmartThemeBlurTintColor, #1e293b) 70%, transparent);
                    --rp-code-bg: color-mix(in srgb, var(--SmartThemeBlurTintColor, #000000) 76%, #000000 24%);
                    --rp-border: var(--SmartThemeBorderColor, rgba(255, 255, 255, 0.1));
                    --rp-accent: var(--SmartThemeQuoteColor, #38bdf8);
                    --rp-accent-strong: var(--SmartThemeEmColor, #00b4d8);
                    --rp-success: #34d399;
                    --rp-danger: #f87171;
                    pointer-events: none;
                    color-scheme: dark;
                }
                .rp-theme-root[data-rp-theme="light"] {
                    --rp-text: #172033;
                    --rp-muted-text: #475569;
                    --rp-dim-text: #64748b;
                    --rp-panel-bg: rgba(248, 250, 252, 0.96);
                    --rp-panel-strong-bg: rgba(241, 245, 249, 0.94);
                    --rp-card-bg: rgba(255, 255, 255, 0.78);
                    --rp-code-bg: rgba(226, 232, 240, 0.74);
                    --rp-border: rgba(15, 23, 42, 0.14);
                    --rp-accent: #0369a1;
                    --rp-accent-strong: #0284c7;
                    color-scheme: light;
                }
                .rp-theme-root[data-rp-theme="dark"] {
                    color-scheme: dark;
                }
                @media (prefers-color-scheme: light) {
                    .rp-theme-root[data-rp-theme="system"] {
                        --rp-text: #172033;
                        --rp-muted-text: #475569;
                        --rp-dim-text: #64748b;
                        --rp-panel-bg: rgba(248, 250, 252, 0.96);
                        --rp-panel-strong-bg: rgba(241, 245, 249, 0.94);
                        --rp-card-bg: rgba(255, 255, 255, 0.78);
                        --rp-code-bg: rgba(226, 232, 240, 0.74);
                        --rp-border: rgba(15, 23, 42, 0.14);
                        --rp-accent: #0369a1;
                        --rp-accent-strong: #0284c7;
                        color-scheme: light;
                    }
                }
                .rp-floating-btn {
                    position: absolute;
                    bottom: 85px;
                    right: 20px;
                    width: 48px;
                    height: 48px;
                    padding: 0;
                    box-sizing: border-box;
                    border-radius: 50%;
                    background: linear-gradient(135deg, #00b4d8, #0077b6);
                    border: 1px solid rgba(255, 255, 255, 0.2);
                    box-shadow: 0 4px 15px rgba(0, 0, 0, 0.4);
                    color: #ffffff;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    cursor: pointer;
                    z-index: 9999;
                    pointer-events: auto;
                    touch-action: none;
                    user-select: none;
                    -webkit-user-select: none;
                    transition: left 220ms cubic-bezier(0.22, 1, 0.36, 1), top 220ms cubic-bezier(0.22, 1, 0.36, 1), right 220ms cubic-bezier(0.22, 1, 0.36, 1), bottom 220ms cubic-bezier(0.22, 1, 0.36, 1), transform 160ms ease, box-shadow 160ms ease;
                }
                .rp-floating-btn.rp-is-dragging,
                .rp-left-floating-btn.rp-is-dragging {
                    cursor: grabbing;
                    transition: none;
                    transform: none;
                }
                .rp-floating-btn:hover {
                    transform: scale(1.1) translateY(-2px);
                    box-shadow: 0 6px 20px rgba(0, 180, 216, 0.5);
                    background: linear-gradient(135deg, #90e0ef, #00b4d8);
                }
                .rp-floating-btn:focus-visible,
                .rp-left-floating-btn:focus-visible {
                    outline: 3px solid #8ac9ff;
                    outline-offset: 3px;
                }
                @media (prefers-reduced-motion: reduce) {
                    .rp-floating-btn, .rp-left-floating-btn { transition: none; }
                }
                .rp-floating-btn .badge {
                    position: absolute;
                    top: -4px;
                    right: -4px;
                    background: #ff4d4f;
                    color: white;
                    border-radius: 10px;
                    padding: 1px 6px;
                    font-size: 10px;
                    font-weight: bold;
                    border: 1px solid rgba(255,255,255,0.5);
                    box-shadow: 0 2px 5px rgba(0,0,0,0.3);
                }
                .rp-notification-badge {
                    min-width: 17px;
                    height: 17px;
                    padding: 0 5px;
                    border-radius: 999px;
                    background: #ef4444;
                    color: #ffffff;
                    font-size: 0.64rem;
                    font-weight: 700;
                    line-height: 1;
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    border: 1px solid rgba(255, 255, 255, 0.35);
                    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.32);
                    flex-shrink: 0;
                    font-variant-numeric: tabular-nums;
                }
                .rp-floating-btn .ws-status {
                    position: absolute;
                    bottom: -2px;
                    left: -2px;
                    width: 10px;
                    height: 10px;
                    border-radius: 50%;
                    border: 2px solid rgba(18, 18, 24, 0.95);
                    box-shadow: 0 1px 3px rgba(0,0,0,0.3);
                }
                .rp-floating-btn .ws-status.connected { background: #34d399; }
                .rp-floating-btn .ws-status.disconnected { background: #f87171; }
                .rp-floating-btn .ws-status.connecting { background: #fbbf24; animation: rp-blink 1s infinite; }
                .rp-drawer {
                    position: fixed;
                    top: 0;
                    right: 0;
                    width: 420px;
                    height: 100vh;
                    background: rgba(18, 18, 24, 0.95);
                    backdrop-filter: blur(15px);
                    -webkit-backdrop-filter: blur(15px);
                    border-left: 1px solid rgba(255, 255, 255, 0.08);
                    box-shadow: -5px 0 25px rgba(0, 0, 0, 0.5);
                    z-index: 10000;
                    display: flex;
                    flex-direction: column;
                    color: #e2e8f0;
                    font-family: system-ui, -apple-system, sans-serif;
                    pointer-events: auto;
                    transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1);
                }
                .rp-drawer-header {
                    padding: 18px 20px;
                    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
                    display: flex;
                    align-items: center;
                    justify-content: space-between;
                    background: rgba(0, 0, 0, 0.2);
                }
                .rp-drawer-title-container {
                    display: flex;
                    flex-direction: column;
                    max-width: 70%;
                }
                .rp-drawer-title {
                    margin: 0;
                    font-size: 1.05rem;
                    font-weight: 600;
                    color: #f8fafc;
                    display: flex;
                    align-items: center;
                    gap: 8px;
                }
                .rp-drawer-subtitle {
                    font-size: 0.72rem;
                    color: #475569;
                    margin-top: 3px;
                    white-space: nowrap;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    font-family: 'SF Mono', 'Fira Code', monospace;
                }
                .rp-actions {
                    display: flex;
                    align-items: center;
                    gap: 8px;
                }
                .rp-history-controls {
                    display: grid;
                    grid-template-columns: minmax(86px, 0.7fr) minmax(0, 1fr);
                    gap: 8px;
                    align-items: center;
                    background: var(--rp-card-bg, rgba(15, 23, 42, 0.62));
                    border: 1px solid var(--rp-border, rgba(148, 163, 184, 0.12));
                    border-radius: 8px;
                    padding: 10px;
                }
                .rp-history-controls input,
                .rp-history-controls select {
                    min-width: 0;
                    height: 30px;
                    border-radius: 6px;
                    border: 1px solid var(--rp-border, rgba(148, 163, 184, 0.12));
                    background: var(--rp-code-bg, rgba(2, 6, 23, 0.5));
                    color: var(--rp-text, #e2e8f0);
                    font-size: 0.74rem;
                    padding: 5px 8px;
                    outline: none;
                }
                .rp-history-actions {
                    grid-column: 1 / -1;
                    display: flex;
                    align-items: center;
                    justify-content: space-between;
                    gap: 8px;
                    color: var(--rp-muted-text, #64748b);
                    font-size: 0.72rem;
                }
                .rp-history-actions button {
                    height: 28px;
                }
                .rp-close-btn {
                    background: none;
                    border: none;
                    color: #475569;
                    font-size: 1.1rem;
                    cursor: pointer;
                    padding: 6px;
                    border-radius: 6px;
                    transition: all 0.2s;
                    width: 32px;
                    height: 32px;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                }
                .rp-close-btn:hover {
                    color: #f1f5f9;
                    background: rgba(255, 255, 255, 0.08);
                }
                .rp-clear-btn {
                    background: rgba(239, 68, 68, 0.1);
                    border: 1px solid rgba(239, 68, 68, 0.2);
                    color: #f87171;
                    font-size: 0.7rem;
                    font-weight: 500;
                    padding: 5px 10px;
                    border-radius: 6px;
                    cursor: pointer;
                    transition: all 0.2s;
                }
                .rp-clear-btn:hover {
                    background: rgba(239, 68, 68, 0.2);
                    border-color: rgba(239, 68, 68, 0.4);
                }
                .rp-drawer-body {
                    flex: 1;
                    overflow-y: auto;
                    padding: 16px;
                    display: flex;
                    flex-direction: column;
                    gap: 12px;
                    scroll-behavior: smooth;
                }
                .rp-session-card {
                    background: rgba(15, 23, 42, 0.62);
                    border: 1px solid rgba(148, 163, 184, 0.12);
                    border-radius: 8px;
                    padding: 12px;
                    display: flex;
                    flex-direction: column;
                    gap: 10px;
                }
                .rp-session-row {
                    display: flex;
                    align-items: center;
                    justify-content: space-between;
                    gap: 10px;
                }
                .rp-session-label {
                    color: #94a3b8;
                    font-size: 0.72rem;
                    text-transform: uppercase;
                    letter-spacing: 0.04em;
                    font-weight: 600;
                }
                .rp-session-value {
                    color: #e2e8f0;
                    font-size: 0.78rem;
                    font-family: 'SF Mono', 'Fira Code', monospace;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                    max-width: 260px;
                }
                .rp-usage-bar {
                    width: 100%;
                    height: 8px;
                    background: rgba(15, 23, 42, 0.9);
                    border: 1px solid rgba(148, 163, 184, 0.1);
                    border-radius: 999px;
                    overflow: hidden;
                }
                .rp-session-heading-row {
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    min-width: 0;
                    margin-top: 4px;
                }
                .rp-session-heading-row .rp-drawer-subtitle {
                    margin-top: 0;
                    min-width: 0;
                }
                .rp-hermes-session-badge {
                    display: inline-flex;
                    align-items: center;
                    gap: 5px;
                    flex: 0 0 auto;
                    padding: 2px 6px;
                    border: 1px solid rgba(148, 163, 184, 0.24);
                    border-radius: 5px;
                    font-size: 0.66rem;
                    line-height: 1.2;
                    color: #cbd5e1;
                    background: rgba(30, 41, 59, 0.7);
                }
                .rp-hermes-session-badge.status-active { color: #86efac; border-color: rgba(74, 222, 128, 0.3); background: rgba(20, 83, 45, 0.28); }
                .rp-hermes-session-badge.status-working { color: #7dd3fc; border-color: rgba(56, 189, 248, 0.3); background: rgba(14, 116, 144, 0.24); }
                .rp-hermes-session-badge.status-rebuilding { color: #c4b5fd; border-color: rgba(167, 139, 250, 0.3); background: rgba(91, 33, 182, 0.22); }
                .rp-hermes-session-badge.status-expired { color: #fcd34d; border-color: rgba(251, 191, 36, 0.32); background: rgba(146, 64, 14, 0.24); }
                .rp-hermes-session-badge.status-not_started { color: #94a3b8; }
                .rp-hermes-session-badge.status-unavailable { color: #fca5a5; border-color: rgba(248, 113, 113, 0.3); background: rgba(127, 29, 29, 0.22); }
                .rp-usage-fill {
                    height: 100%;
                    width: 0%;
                    border-radius: inherit;
                    background: linear-gradient(90deg, #22c55e, #38bdf8);
                    transition: width 0.25s ease;
                }
                .rp-usage-fill.warn {
                    background: linear-gradient(90deg, #f59e0b, #f97316);
                }
                .rp-usage-fill.danger {
                    background: linear-gradient(90deg, #f97316, #ef4444);
                }
                .rp-streaming-estimate {
                    font-size: 0.7rem;
                    color: #38bdf8;
                    text-align: center;
                    margin-top: 4px;
                    animation: rp-pulse 1.5s ease-in-out infinite;
                }
                @keyframes rp-pulse {
                    0%, 100% { opacity: 1; }
                    50% { opacity: 0.5; }
                }
                .rp-context-critical {
                    color: #ef4444 !important;
                    font-weight: 600;
                }
                .rp-context-warn {
                    color: #f59e0b !important;
                    font-weight: 600;
                }
                .rp-model-select {
                    width: 100%;
                    min-height: 34px;
                    background: rgba(2, 6, 23, 0.64);
                    border: 1px solid rgba(148, 163, 184, 0.16);
                    border-radius: 6px;
                    color: #e2e8f0;
                    padding: 6px 8px;
                    font-size: 0.78rem;
                    outline: none;
                }
                .rp-model-select:disabled {
                    color: #64748b;
                    cursor: wait;
                }
                .rp-session-grid {
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(70px, 1fr));
                    gap: 8px;
                }
                .rp-metric {
                    background: rgba(2, 6, 23, 0.42);
                    border: 1px solid rgba(148, 163, 184, 0.08);
                    border-radius: 6px;
                    padding: 8px;
                    min-width: 0;
                }
                .rp-metric .rp-session-value {
                    display: block;
                    margin-top: 4px;
                    max-width: none;
                }
                .rp-session-actions {
                    display: flex;
                    align-items: center;
                    gap: 8px;
                }
                .rp-agent-controls {
                    display: grid;
                    grid-template-columns: repeat(6, minmax(0, 1fr));
                    gap: 8px;
                }
                .rp-agent-control-btn {
                    min-width: 0;
                    height: 32px;
                    border-radius: 6px;
                    border: 1px solid rgba(148, 163, 184, 0.12);
                    background: rgba(2, 6, 23, 0.5);
                    color: #cbd5e1;
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    gap: 6px;
                    cursor: pointer;
                    transition: all 0.2s ease;
                    font-size: 0.72rem;
                    font-weight: 600;
                    overflow: hidden;
                }
                .rp-agent-control-btn span {
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }
                .rp-agent-control-btn:hover:not(:disabled) {
                    color: #f8fafc;
                    border-color: rgba(56, 189, 248, 0.35);
                    background: rgba(14, 116, 144, 0.18);
                }
                .rp-agent-control-btn.danger:hover:not(:disabled) {
                    color: #fecaca;
                    border-color: rgba(248, 113, 113, 0.35);
                    background: rgba(127, 29, 29, 0.22);
                }
                .rp-agent-control-btn:disabled {
                    opacity: 0.45;
                    cursor: not-allowed;
                }
                .rp-agent-control-feedback {
                    border-radius: 6px;
                    border: 1px solid rgba(56, 189, 248, 0.18);
                    background: rgba(14, 116, 144, 0.12);
                    color: #bae6fd;
                    padding: 7px 9px;
                    font-size: 0.74rem;
                    line-height: 1.35;
                    display: flex;
                    align-items: center;
                    gap: 7px;
                }
                .rp-agent-control-feedback.error {
                    color: #fecaca;
                    border-color: rgba(248, 113, 113, 0.24);
                    background: rgba(127, 29, 29, 0.16);
                }
                .rp-persona-panel {
                    border-radius: 6px;
                    border: 1px solid rgba(56, 189, 248, 0.18);
                    background: rgba(2, 6, 23, 0.34);
                    padding: 10px;
                    display: flex;
                    flex-direction: column;
                    gap: 10px;
                }
                .rp-persona-header {
                    display: flex;
                    align-items: flex-start;
                    justify-content: space-between;
                    gap: 10px;
                }
                .rp-persona-title {
                    color: #e2e8f0;
                    font-size: 0.82rem;
                    font-weight: 700;
                    display: flex;
                    align-items: center;
                    gap: 7px;
                }
                .rp-persona-subtitle {
                    color: #94a3b8;
                    font-size: 0.72rem;
                    margin-top: 3px;
                }
                .rp-persona-editor {
                    width: 100%;
                    min-height: 190px;
                    resize: vertical;
                    background: rgba(2, 6, 23, 0.7);
                    border: 1px solid rgba(148, 163, 184, 0.16);
                    border-radius: 6px;
                    color: #e2e8f0;
                    padding: 8px;
                    font-size: 0.76rem;
                    line-height: 1.45;
                    outline: none;
                    box-sizing: border-box;
                }
                .rp-persona-editor:focus {
                    border-color: rgba(56, 189, 248, 0.38);
                }
                .rp-persona-actions {
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(92px, 1fr));
                    gap: 8px;
                }
                .rp-persona-pending {
                    border-radius: 6px;
                    border: 1px solid rgba(250, 204, 21, 0.22);
                    background: rgba(113, 63, 18, 0.18);
                    padding: 9px;
                    display: flex;
                    flex-direction: column;
                    gap: 7px;
                }
                .rp-persona-pending-title {
                    color: #fde68a;
                    font-size: 0.78rem;
                    font-weight: 700;
                    display: flex;
                    align-items: center;
                    gap: 7px;
                }
                .rp-persona-pending-summary,
                .rp-persona-pending-reason {
                    color: #fef3c7;
                    font-size: 0.72rem;
                    line-height: 1.35;
                }
                .rp-persona-preview {
                    max-height: 130px;
                    overflow: auto;
                    white-space: pre-wrap;
                    border-radius: 6px;
                    background: rgba(2, 6, 23, 0.48);
                    border: 1px solid rgba(148, 163, 184, 0.12);
                    color: #e2e8f0;
                    padding: 8px;
                    font-size: 0.72rem;
                    line-height: 1.4;
                }
                .rp-persona-history {
                    display: flex;
                    flex-direction: column;
                    gap: 8px;
                }
                .rp-icon-btn {
                    width: 30px;
                    height: 30px;
                    border-radius: 6px;
                    border: 1px solid rgba(148, 163, 184, 0.12);
                    background: rgba(2, 6, 23, 0.5);
                    color: #cbd5e1;
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    cursor: pointer;
                    transition: all 0.2s ease;
                    flex: 0 0 auto;
                }
                .rp-icon-btn:hover {
                    color: #f8fafc;
                    border-color: rgba(56, 189, 248, 0.35);
                    background: rgba(14, 116, 144, 0.18);
                }
                .rp-icon-btn.danger:hover {
                    color: #fecaca;
                    border-color: rgba(248, 113, 113, 0.35);
                    background: rgba(127, 29, 29, 0.22);
                }
                .rp-muted {
                    color: #64748b;
                    font-size: 0.74rem;
                    line-height: 1.35;
                }
                .rp-status-strip {
                    position: sticky;
                    top: 0;
                    z-index: 1;
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    color: #bae6fd;
                    background: rgba(14, 116, 144, 0.14);
                    border: 1px solid rgba(56, 189, 248, 0.18);
                    border-radius: 8px;
                    padding: 8px 10px;
                    font-size: 0.78rem;
                    line-height: 1.3;
                }
                .responses-proxy-status-indicator {
                    position: sticky;
                    bottom: 10px;
                    margin: 10px;
                    opacity: 0.9;
                    text-shadow: 0px 0px calc(var(--shadowWidth) * 1px) var(--SmartThemeShadowColor);
                    order: 9999;
                    color: var(--SmartThemeBodyColor);
                    display: flex;
                    align-items: center;
                    gap: 7px;
                }
                .responses-proxy-status-indicator .rp-dots {
                    display: inline-flex;
                    gap: 4px;
                }
                .responses-proxy-status-indicator .rp-dots span {
                    width: 5px;
                    height: 5px;
                    border-radius: 50%;
                    background: currentColor;
                    animation: rp-dot-fade 1.2s ease-in-out infinite;
                }
                .responses-proxy-status-indicator .rp-dots span:nth-child(2) { animation-delay: 0.2s; }
                .responses-proxy-status-indicator .rp-dots span:nth-child(3) { animation-delay: 0.4s; }
                .rp-drawer-body::-webkit-scrollbar {
                    width: 6px;
                }
                .rp-drawer-body::-webkit-scrollbar-track {
                    background: transparent;
                }
                .rp-drawer-body::-webkit-scrollbar-thumb {
                    background: rgba(255, 255, 255, 0.1);
                    border-radius: 3px;
                }
                .rp-drawer-body::-webkit-scrollbar-thumb:hover {
                    background: rgba(255, 255, 255, 0.2);
                }
                .rp-empty-state {
                    display: flex;
                    flex-direction: column;
                    align-items: center;
                    justify-content: center;
                    height: 100%;
                    min-height: 300px;
                    color: #334155;
                    text-align: center;
                    gap: 12px;
                }
                .rp-empty-state i {
                    font-size: 2.5rem;
                }
                .rp-call-card {
                    background: rgba(30, 41, 59, 0.3);
                    border: 1px solid rgba(255, 255, 255, 0.04);
                    border-radius: 10px;
                    padding: 14px 16px;
                    display: flex;
                    flex-direction: column;
                    gap: 10px;
                    transition: all 0.25s ease;
                    animation: rp-card-in 0.35s cubic-bezier(0.16, 1, 0.3, 1);
                }
                .rp-call-card:hover {
                    border-color: rgba(0, 180, 216, 0.2);
                    background: rgba(30, 41, 59, 0.45);
                }
                .rp-call-card.newest {
                    border-color: rgba(0, 180, 216, 0.25);
                    background: rgba(0, 180, 216, 0.06);
                }
                .rp-card-header {
                    display: flex;
                    align-items: center;
                    justify-content: space-between;
                }
                .rp-tool-name {
                    font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
                    font-weight: 600;
                    font-size: 0.88rem;
                    color: #38bdf8;
                    display: flex;
                    align-items: center;
                    gap: 7px;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                    max-width: 250px;
                }
                .rp-tool-name i {
                    flex-shrink: 0;
                    font-size: 0.82rem;
                }
                .rp-status-badge {
                    font-size: 0.62rem;
                    padding: 2px 8px;
                    border-radius: 10px;
                    font-weight: 600;
                    text-transform: uppercase;
                    letter-spacing: 0.03em;
                    flex-shrink: 0;
                }
                .rp-status-running {
                    background: rgba(14, 165, 233, 0.12);
                    color: #38bdf8;
                    border: 1px solid rgba(14, 165, 233, 0.2);
                }
                .rp-status-completed {
                    background: rgba(16, 185, 129, 0.12);
                    color: #34d399;
                    border: 1px solid rgba(16, 185, 129, 0.2);
                }
                .rp-code-container {
                    display: flex;
                    flex-direction: column;
                    gap: 4px;
                }
                .rp-code-label {
                    font-size: 0.65rem;
                    color: #334155;
                    display: flex;
                    align-items: center;
                    gap: 4px;
                    text-transform: uppercase;
                    letter-spacing: 0.04em;
                    font-weight: 500;
                }
                .rp-code-block {
                    background: rgba(0, 0, 0, 0.25);
                    border: 1px solid rgba(255, 255, 255, 0.03);
                    border-radius: 6px;
                    padding: 10px 12px;
                    font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
                    font-size: 0.75rem;
                    color: #cbd5e1;
                    white-space: pre-wrap;
                    word-break: break-all;
                    line-height: 1.5;
                    max-height: 140px;
                    overflow-y: auto;
                    margin: 0;
                }
                .rp-code-block::-webkit-scrollbar {
                    width: 4px;
                }
                .rp-code-block::-webkit-scrollbar-thumb {
                    background: rgba(255, 255, 255, 0.08);
                    border-radius: 2px;
                }
                .rp-timestamp {
                    font-size: 0.62rem;
                    color: #334155;
                    align-self: flex-end;
                    font-family: 'SF Mono', monospace;
                    display: flex;
                    align-items: center;
                    gap: 4px;
                }
                .rp-timestamp i {
                    font-size: 0.55rem;
                }
                .rp-output-block .rp-code-label {
                    color: #34d399;
                }
                .rp-output-block .rp-code-block {
                    background: rgba(0, 0, 0, 0.35);
                }
                @keyframes rp-card-in {
                    from {
                        opacity: 0;
                        transform: translateY(-12px) scale(0.98);
                    }
                    to {
                        opacity: 1;
                        transform: translateY(0) scale(1);
                    }
                }
                @keyframes rp-pulse {
                    0%, 100% { box-shadow: 0 0 0 0 rgba(0, 180, 216, 0.3); }
                    50% { box-shadow: 0 0 0 6px rgba(0, 180, 216, 0); }
                }
                @keyframes rp-blink {
                    0%, 100% { opacity: 1; }
                    50% { opacity: 0.4; }
                }
                @keyframes rp-dot-fade {
                    0%, 100% { opacity: 0.25; transform: translateY(0); }
                    35% { opacity: 1; transform: translateY(-2px); }
                    70% { opacity: 0.45; transform: translateY(0); }
                }
                .rp-floating-btn.pulse {
                    animation: rp-pulse 1.5s ease-in-out;
                }
                .rp-floating-btn.attention {
                    background: linear-gradient(135deg, #b91c1c, #d97706);
                    box-shadow: 0 4px 20px rgba(217, 119, 6, 0.6);
                    animation: rp-pulse-attention 1.5s infinite;
                }
                @keyframes rp-pulse-attention {
                    0%, 100% { box-shadow: 0 0 0 0 rgba(217, 119, 6, 0.4); }
                    50% { box-shadow: 0 0 0 10px rgba(217, 119, 6, 0); }
                }
                .rp-clarify-card {
                    background: rgba(147, 51, 234, 0.08);
                    border: 1px solid rgba(147, 51, 234, 0.25);
                    border-radius: 10px;
                    padding: 14px 16px;
                    display: flex;
                    flex-direction: column;
                    gap: 12px;
                    animation: rp-card-in 0.35s cubic-bezier(0.16, 1, 0.3, 1);
                    margin-bottom: 12px;
                }
                .rp-clarify-title {
                    font-size: 0.88rem;
                    font-weight: 600;
                    color: #c084fc;
                    display: flex;
                    align-items: center;
                    gap: 7px;
                }
                .rp-clarify-question {
                    font-size: 0.85rem;
                    line-height: 1.4;
                    color: #e2e8f0;
                }
                .rp-clarify-choices {
                    display: flex;
                    flex-direction: column;
                    gap: 8px;
                }
                .rp-clarify-choice-btn {
                    background: rgba(147, 51, 234, 0.12);
                    border: 1px solid rgba(147, 51, 234, 0.2);
                    border-radius: 6px;
                    color: #e9d5ff;
                    padding: 8px 12px;
                    font-size: 0.78rem;
                    text-align: left;
                    cursor: pointer;
                    transition: all 0.2s ease;
                }
                .rp-clarify-choice-btn:hover {
                    background: rgba(147, 51, 234, 0.25);
                    border-color: rgba(147, 51, 234, 0.45);
                    transform: translateY(-1px);
                }
                .rp-clarify-choice-btn.selected {
                    background: rgba(147, 51, 234, 0.32);
                    border-color: rgba(192, 132, 252, 0.7);
                }
                .rp-clarify-choice-btn:disabled,
                .rp-clarify-send-btn:disabled {
                    cursor: not-allowed;
                    opacity: 0.55;
                    transform: none;
                }
                .rp-clarify-input-container {
                    display: flex;
                    gap: 8px;
                    margin-top: 4px;
                }
                .rp-clarify-input {
                    flex: 1;
                    background: rgba(0, 0, 0, 0.25);
                    border: 1px solid rgba(147, 51, 234, 0.2);
                    border-radius: 6px;
                    color: #f3e8ff;
                    padding: 6px 10px;
                    font-size: 0.78rem;
                    outline: none;
                }
                .rp-clarify-input:focus {
                    border-color: rgba(147, 51, 234, 0.5);
                    box-shadow: 0 0 5px rgba(147, 51, 234, 0.25);
                }
                .rp-clarify-send-btn {
                    background: #a855f7;
                    border: none;
                    border-radius: 6px;
                    color: white;
                    padding: 6px 12px;
                    font-size: 0.78rem;
                    font-weight: 500;
                    cursor: pointer;
                    transition: all 0.2s ease;
                }
                .rp-clarify-send-btn:hover {
                    background: #c084fc;
                    transform: translateY(-1px);
                }
                .rp-approval-card {
                    background: rgba(249, 115, 22, 0.08);
                    border: 1px solid rgba(249, 115, 22, 0.25);
                    border-radius: 10px;
                    padding: 14px 16px;
                    display: flex;
                    flex-direction: column;
                    gap: 10px;
                    animation: rp-card-in 0.35s cubic-bezier(0.16, 1, 0.3, 1);
                    margin-bottom: 12px;
                }
                .rp-approval-title {
                    font-size: 0.88rem;
                    font-weight: 600;
                    color: #fdba74;
                    display: flex;
                    align-items: center;
                    gap: 7px;
                }
                .rp-approval-desc {
                    font-size: 0.8rem;
                    color: #ffedd5;
                    line-height: 1.35;
                }
                .rp-approval-cmd {
                    background: rgba(0, 0, 0, 0.3);
                    border: 1px solid rgba(249, 115, 22, 0.15);
                    border-radius: 6px;
                    padding: 8px 10px;
                    font-family: 'SF Mono', 'Fira Code', monospace;
                    font-size: 0.74rem;
                    color: #fed7aa;
                    word-break: break-all;
                    max-height: 100px;
                    overflow-y: auto;
                }
                .rp-approval-actions {
                    display: flex;
                    flex-wrap: wrap;
                    gap: 8px;
                    margin-top: 4px;
                }
                .rp-approval-btn {
                    flex: 1;
                    min-width: 80px;
                    border-radius: 6px;
                    padding: 6px 10px;
                    font-size: 0.74rem;
                    font-weight: 500;
                    cursor: pointer;
                    transition: all 0.2s ease;
                    text-align: center;
                }
                .rp-approval-btn.once {
                    background: rgba(34, 197, 94, 0.15);
                    border: 1px solid rgba(34, 197, 94, 0.3);
                    color: #86efac;
                }
                .rp-approval-btn.once:hover {
                    background: rgba(34, 197, 94, 0.28);
                    border-color: rgba(34, 197, 94, 0.5);
                    transform: translateY(-1px);
                }
                .rp-approval-btn.session {
                    background: rgba(56, 189, 248, 0.15);
                    border: 1px solid rgba(56, 189, 248, 0.3);
                    color: #bae6fd;
                }
                .rp-approval-btn.session:hover {
                    background: rgba(56, 189, 248, 0.28);
                    border-color: rgba(56, 189, 248, 0.5);
                    transform: translateY(-1px);
                }
                .rp-approval-btn.always {
                    background: rgba(168, 85, 247, 0.15);
                    border: 1px solid rgba(168, 85, 247, 0.3);
                    color: #e9d5ff;
                }
                .rp-approval-btn.always:hover {
                    background: rgba(168, 85, 247, 0.28);
                    border-color: rgba(168, 85, 247, 0.5);
                    transform: translateY(-1px);
                }
                .rp-approval-btn.deny {
                    background: rgba(239, 68, 68, 0.15);
                    border: 1px solid rgba(239, 68, 68, 0.3);
                    color: #fca5a5;
                }
                .rp-approval-btn.deny:hover {
                    background: rgba(239, 68, 68, 0.28);
                    border-color: rgba(239, 68, 68, 0.5);
                    transform: translateY(-1px);
                }
                .rp-sudo-card {
                    background: rgba(14, 165, 233, 0.08);
                    border: 1px solid rgba(14, 165, 233, 0.28);
                    border-radius: 10px;
                    padding: 14px 16px;
                    display: flex;
                    flex-direction: column;
                    gap: 10px;
                    animation: rp-card-in 0.35s cubic-bezier(0.16, 1, 0.3, 1);
                    margin-bottom: 12px;
                }
                .rp-sudo-title {
                    font-size: 0.88rem;
                    font-weight: 600;
                    color: #7dd3fc;
                    display: flex;
                    align-items: center;
                    gap: 7px;
                }
                .rp-sudo-desc {
                    font-size: 0.8rem;
                    color: #dbeafe;
                    line-height: 1.35;
                }
                .rp-sudo-input-row {
                    display: flex;
                    gap: 8px;
                    align-items: stretch;
                }
                .rp-sudo-input {
                    flex: 1;
                    min-width: 0;
                    background: rgba(0, 0, 0, 0.25);
                    border: 1px solid rgba(14, 165, 233, 0.25);
                    border-radius: 6px;
                    color: #f8fafc;
                    padding: 7px 10px;
                    font-size: 0.78rem;
                    outline: none;
                }
                .rp-sudo-input:focus {
                    border-color: rgba(14, 165, 233, 0.55);
                    box-shadow: 0 0 5px rgba(14, 165, 233, 0.25);
                }
                .rp-sudo-actions {
                    display: flex;
                    justify-content: flex-end;
                }
                .rp-sudo-btn {
                    border-radius: 6px;
                    padding: 7px 12px;
                    font-size: 0.74rem;
                    font-weight: 500;
                    cursor: pointer;
                    transition: all 0.2s ease;
                    text-align: center;
                    border: 1px solid transparent;
                }
                .rp-sudo-btn:disabled {
                    opacity: 0.45;
                    cursor: not-allowed;
                    transform: none;
                }
                .rp-sudo-btn.submit {
                    background: rgba(34, 197, 94, 0.16);
                    border-color: rgba(34, 197, 94, 0.35);
                    color: #bbf7d0;
                    min-width: 84px;
                }
                .rp-sudo-btn.submit:not(:disabled):hover {
                    background: rgba(34, 197, 94, 0.3);
                    border-color: rgba(34, 197, 94, 0.55);
                    transform: translateY(-1px);
                }
                .rp-sudo-btn.skip {
                    background: rgba(148, 163, 184, 0.12);
                    border-color: rgba(148, 163, 184, 0.25);
                    color: #cbd5e1;
                }
                .rp-sudo-btn.skip:not(:disabled):hover {
                    background: rgba(148, 163, 184, 0.22);
                    border-color: rgba(148, 163, 184, 0.4);
                    transform: translateY(-1px);
                }
                @media (max-width: 520px) {
                    .rp-sudo-input-row {
                        flex-direction: column;
                    }
                    .rp-sudo-btn.submit {
                        width: 100%;
                    }
                }

                /* Left panel styles */
                .rp-left-floating-btn {
                    position: absolute;
                    bottom: 85px;
                    left: 20px;
                    width: 48px;
                    height: 48px;
                    padding: 0;
                    box-sizing: border-box;
                    border-radius: 50%;
                    background: linear-gradient(135deg, #00b4d8, #0077b6);
                    border: 1px solid rgba(255, 255, 255, 0.2);
                    box-shadow: 0 4px 15px rgba(0, 0, 0, 0.4);
                    color: #ffffff;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    cursor: pointer;
                    z-index: 9999;
                    pointer-events: auto;
                    touch-action: none;
                    user-select: none;
                    -webkit-user-select: none;
                    transition: left 220ms cubic-bezier(0.22, 1, 0.36, 1), top 220ms cubic-bezier(0.22, 1, 0.36, 1), right 220ms cubic-bezier(0.22, 1, 0.36, 1), bottom 220ms cubic-bezier(0.22, 1, 0.36, 1), transform 160ms ease, box-shadow 160ms ease;
                }
                .rp-left-floating-btn:hover {
                    transform: scale(1.1) translateY(-2px);
                    box-shadow: 0 6px 20px rgba(0, 180, 216, 0.5);
                    background: linear-gradient(135deg, #90e0ef, #00b4d8);
                }
                .rp-left-floating-btn .rp-notification-badge {
                    position: absolute;
                    top: -4px;
                    right: -5px;
                }
                .rp-left-floating-btn.pulse {
                    animation: rp-pulse 1.5s ease-in-out;
                }

                .rp-left-drawer {
                    position: fixed;
                    top: 0;
                    left: 0;
                    width: 500px;
                    max-width: 90vw;
                    height: 100vh;
                    background: rgba(18, 18, 24, 0.95);
                    backdrop-filter: blur(15px);
                    -webkit-backdrop-filter: blur(15px);
                    border-right: 1px solid rgba(255, 255, 255, 0.08);
                    box-shadow: 5px 0 25px rgba(0, 0, 0, 0.5);
                    z-index: 10000;
                    display: flex;
                    flex-direction: column;
                    color: #e2e8f0;
                    font-family: system-ui, -apple-system, sans-serif;
                    pointer-events: auto;
                    transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1);
                }

                .rp-left-tabs {
                    display: flex;
                    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
                    background: rgba(0, 0, 0, 0.15);
                    padding: 0 10px;
                    overflow-x: auto;
                }

                .rp-left-tab {
                    flex: 1 0 104px;
                    background: none;
                    border: none;
                    color: #94a3b8;
                    padding: 12px 6px;
                    font-size: 0.76rem;
                    font-weight: 500;
                    cursor: pointer;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    gap: 6px;
                    border-bottom: 2px solid transparent;
                    transition: all 0.2s;
                    min-width: 0;
                    white-space: nowrap;
                }

                .rp-left-tab:hover {
                    color: #f8fafc;
                    background: rgba(255, 255, 255, 0.02);
                }

                .rp-left-tab.active {
                    color: #38bdf8;
                    border-bottom-color: #38bdf8;
                    background: rgba(56, 189, 248, 0.04);
                }

                .rp-left-tab .rp-notification-badge {
                    background: rgba(239, 68, 68, 0.92);
                    font-size: 0.58rem;
                    min-width: 16px;
                    height: 16px;
                    padding: 0 4px;
                    margin-left: 1px;
                }

                .rp-left-tab.active .rp-notification-badge {
                    background: #38bdf8;
                    color: #082f49;
                    border-color: rgba(186, 230, 253, 0.5);
                }

                /* Console tab styling */
                .rp-console-container {
                    display: flex;
                    flex-direction: column;
                    gap: 16px;
                    padding: 16px;
                    height: 100%;
                    overflow-y: auto;
                }

                .rp-console-item {
                    background: rgba(0, 0, 0, 0.3);
                    border: 1px solid rgba(255, 255, 255, 0.05);
                    border-radius: 8px;
                    overflow: hidden;
                }

                .rp-console-cmd-header {
                    display: flex;
                    align-items: center;
                    background: rgba(255, 255, 255, 0.03);
                    padding: 8px 12px;
                    font-family: 'SF Mono', 'Fira Code', monospace;
                    font-size: 0.76rem;
                    border-bottom: 1px solid rgba(255, 255, 255, 0.03);
                    gap: 8px;
                }

                .rp-console-prompt {
                    color: #38bdf8;
                    font-weight: bold;
                }

                .rp-console-cmd-text {
                    flex: 1;
                    color: #cbd5e1;
                    font-weight: 500;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }

                .rp-console-status-icon {
                    font-size: 0.76rem;
                }

                .rp-console-output {
                    margin: 0;
                    padding: 12px;
                    font-family: 'SF Mono', 'Fira Code', monospace;
                    font-size: 0.74rem;
                    color: #cbd5e1;
                    background: rgba(0, 0, 0, 0.4);
                    white-space: pre-wrap;
                    word-break: break-all;
                    max-height: 250px;
                    overflow-y: auto;
                }

                .rp-console-output.rp-console-error {
                    color: #f87171;
                    background: rgba(239, 68, 68, 0.05);
                }

                /* Diffs tab styling */
                .rp-diffs-container {
                    display: flex;
                    flex-direction: column;
                    height: 100%;
                    overflow: hidden;
                }

                .rp-diffs-sidebar {
                    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
                    background: rgba(0, 0, 0, 0.1);
                    max-height: 180px;
                    display: flex;
                    flex-direction: column;
                }

                .rp-diffs-sidebar-title {
                    font-size: 0.7rem;
                    text-transform: uppercase;
                    color: #64748b;
                    padding: 8px 16px;
                    font-weight: 600;
                    letter-spacing: 0.04em;
                }

                .rp-diffs-list {
                    overflow-y: auto;
                    display: flex;
                    flex-direction: column;
                    padding: 0 8px 8px;
                    gap: 4px;
                }

                .rp-diff-item-btn {
                    display: flex;
                    align-items: center;
                    background: none;
                    border: 1px solid transparent;
                    border-radius: 6px;
                    padding: 8px 12px;
                    cursor: pointer;
                    text-align: left;
                    color: #94a3b8;
                    font-size: 0.76rem;
                    transition: all 0.2s;
                    width: 100%;
                }

                .rp-diff-item-btn:hover {
                    background: rgba(255, 255, 255, 0.03);
                    color: #e2e8f0;
                }

                .rp-diff-item-btn.active {
                    background: rgba(56, 189, 248, 0.08);
                    border-color: rgba(56, 189, 248, 0.15);
                    color: #38bdf8;
                }

                .rp-diff-filename {
                    flex: 1;
                    font-weight: 500;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }

                .rp-diff-time {
                    font-size: 0.65rem;
                    color: #475569;
                    margin-left: 8px;
                    font-family: monospace;
                }

                .rp-diff-content-panel {
                    flex: 1;
                    display: flex;
                    flex-direction: column;
                    overflow: hidden;
                }

                .rp-diff-content-header {
                    background: rgba(0, 0, 0, 0.15);
                    padding: 10px 16px;
                    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    font-size: 0.74rem;
                }

                .rp-diff-header-title {
                    color: #e2e8f0;
                    font-family: monospace;
                    font-weight: 500;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                    max-width: 70%;
                }

                .rp-diff-header-tool {
                    color: #64748b;
                    font-size: 0.68rem;
                }

                .rp-diff-scroller {
                    flex: 1;
                    overflow: auto;
                    background: rgba(2, 6, 23, 0.4);
                    padding: 10px 0;
                }

                .rp-diff-viewer {
                    display: flex;
                    flex-direction: column;
                }

                .rp-diff-line {
                    display: flex;
                    font-family: 'SF Mono', 'Fira Code', monospace;
                    font-size: 0.72rem;
                    line-height: 1.5;
                    white-space: pre;
                    padding-right: 12px;
                    min-width: max-content;
                }

                .rp-diff-line-num {
                    width: 40px;
                    color: #475569;
                    text-align: right;
                    margin-right: 12px;
                    user-select: none;
                    border-right: 1px solid rgba(255, 255, 255, 0.04);
                    padding-right: 10px;
                    flex-shrink: 0;
                }

                .rp-diff-line-marker {
                    width: 22px;
                    flex-shrink: 0;
                    text-align: center;
                    font-weight: 700;
                    user-select: none;
                }

                .rp-diff-line-content {
                    flex: 1;
                    min-width: max-content;
                    tab-size: 4;
                }

                .rp-diff-line-add {
                    background-color: rgba(34, 197, 94, 0.1);
                    color: #4ade80;
                }

                .rp-diff-line-del {
                    background-color: rgba(239, 68, 68, 0.1);
                    color: #f87171;
                }

                .rp-diff-line-add .rp-diff-line-marker {
                    color: #86efac;
                }

                .rp-diff-line-del .rp-diff-line-marker {
                    color: #fca5a5;
                }

                .rp-diff-line-hunk {
                    background-color: rgba(56, 189, 248, 0.05);
                    color: #38bdf8;
                    font-style: italic;
                }

                .rp-diff-line-header {
                    background-color: rgba(255, 255, 255, 0.03);
                    color: #f8fafc;
                    font-weight: bold;
                }

                .rp-diff-line-note {
                    color: #fbbf24;
                    font-style: italic;
                }

                /* Todos tab styling */
                .rp-todo-container {
                    display: flex;
                    flex-direction: column;
                    padding: 16px;
                    gap: 14px;
                }

                .rp-todo-header {
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                }

                .rp-todo-progress-label {
                    font-size: 0.8rem;
                    font-weight: 600;
                    color: #f8fafc;
                }

                .rp-todo-progress-value {
                    font-size: 0.76rem;
                    font-family: monospace;
                    color: #38bdf8;
                }

                .rp-todo-progress-bar {
                    width: 100%;
                    height: 6px;
                    background: rgba(0, 0, 0, 0.3);
                    border-radius: 3px;
                    overflow: hidden;
                }

                .rp-todo-progress-fill {
                    height: 100%;
                    background: linear-gradient(90deg, #38bdf8, #10b981);
                    transition: width 0.3s ease;
                    border-radius: 3px;
                }

                .rp-todo-list {
                    display: flex;
                    flex-direction: column;
                    gap: 10px;
                    margin-top: 6px;
                }

                .rp-todo-item {
                    display: flex;
                    align-items: flex-start;
                    gap: 10px;
                    padding: 10px 12px;
                    background: rgba(255, 255, 255, 0.02);
                    border: 1px solid rgba(255, 255, 255, 0.03);
                    border-radius: 6px;
                    transition: all 0.2s;
                }

                .rp-todo-item.completed {
                    background: rgba(16, 185, 129, 0.03);
                    border-color: rgba(16, 185, 129, 0.08);
                }

                .rp-todo-item.status-pending {
                    background: rgba(245, 158, 11, 0.03);
                    border-color: rgba(245, 158, 11, 0.08);
                }

                .rp-todo-item.status-in_progress {
                    background: rgba(56, 189, 248, 0.03);
                    border-color: rgba(56, 189, 248, 0.1);
                }

                .rp-todo-item.status-cancelled {
                    background: rgba(249, 115, 22, 0.03);
                    border-color: rgba(249, 115, 22, 0.1);
                }

                .rp-todo-item-checkbox {
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    flex: 0 0 auto;
                    font-size: 0.88rem;
                    margin-top: 1px;
                }

                .rp-todo-item-text {
                    flex: 1 1 auto;
                    min-width: 0;
                    font-size: 0.78rem;
                    line-height: 1.4;
                    color: #cbd5e1;
                }

                .rp-todo-item.completed .rp-todo-item-text {
                    color: #64748b;
                    text-decoration: line-through;
                }

                .rp-todo-item.status-cancelled .rp-todo-item-text {
                    color: #94a3b8;
                    text-decoration: line-through;
                }

                .rp-todo-status-badge {
                    flex: 0 0 auto;
                    max-width: 9rem;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                    padding: 2px 7px;
                    border-radius: 999px;
                    font-size: 0.64rem;
                    font-weight: 700;
                    line-height: 1.2;
                    text-transform: uppercase;
                    color: #94a3b8;
                    background: rgba(148, 163, 184, 0.08);
                    border: 1px solid rgba(148, 163, 184, 0.12);
                }

                .rp-todo-status-badge.status-completed {
                    color: #34d399;
                    background: rgba(16, 185, 129, 0.09);
                    border-color: rgba(16, 185, 129, 0.16);
                }

                .rp-todo-status-badge.status-in_progress {
                    color: #38bdf8;
                    background: rgba(56, 189, 248, 0.09);
                    border-color: rgba(56, 189, 248, 0.16);
                }

                .rp-todo-status-badge.status-pending {
                    color: #f59e0b;
                    background: rgba(245, 158, 11, 0.09);
                    border-color: rgba(245, 158, 11, 0.16);
                }

                .rp-todo-status-badge.status-cancelled {
                    color: #fb923c;
                    background: rgba(249, 115, 22, 0.09);
                    border-color: rgba(249, 115, 22, 0.16);
                }

                /* Workspace lock controls */
                .rp-workspace-lock {
                    flex: 0 0 auto;
                    padding: 9px 12px;
                    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
                    background: rgba(0, 0, 0, 0.14);
                }
                .rp-workspace-lock__status {
                    display: flex;
                    align-items: center;
                    gap: 7px;
                    color: #cbd5e1;
                    font-size: 0.72rem;
                    font-family: 'SF Mono', 'Fira Code', monospace;
                    overflow: hidden;
                }
                .rp-workspace-lock__status i { color: #38bdf8; }
                .rp-workspace-lock__status span,
                .rp-workspace-lock__selection {
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }
                .rp-workspace-lock__selection {
                    color: #94a3b8;
                    font-size: 0.68rem;
                    font-family: 'SF Mono', 'Fira Code', monospace;
                    margin-top: 4px;
                }
                .rp-workspace-lock__actions {
                    display: flex;
                    align-items: center;
                    gap: 7px;
                    margin-top: 8px;
                }
                .rp-workspace-lock__button,
                .rp-workspace-lock__default {
                    border-radius: 6px;
                    padding: 5px 8px;
                    font-size: 0.68rem;
                    cursor: pointer;
                }
                .rp-workspace-lock__button {
                    border: 1px solid rgba(56, 189, 248, 0.35);
                    background: rgba(56, 189, 248, 0.12);
                    color: #7dd3fc;
                }
                .rp-workspace-lock__button:disabled,
                .rp-workspace-lock__default:disabled { opacity: 0.45; cursor: not-allowed; }
                .rp-workspace-lock__default {
                    border: 1px solid rgba(148, 163, 184, 0.22);
                    background: transparent;
                    color: #cbd5e1;
                }
                .rp-workspace-lock__hint {
                    margin-top: 6px;
                    color: #64748b;
                    font-size: 0.65rem;
                    line-height: 1.32;
                }
                .rp-workspace-container {
                    display: flex;
                    flex-direction: column;
                    height: 100%;
                    min-height: 0;
                    overflow: hidden;
                }

                .rp-workspace-toolbar {
                    min-height: 42px;
                    display: flex;
                    align-items: center;
                    justify-content: space-between;
                    gap: 10px;
                    padding: 8px 12px;
                    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
                    background: rgba(0, 0, 0, 0.12);
                }

                .rp-workspace-root {
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    min-width: 0;
                    color: #cbd5e1;
                    font-family: 'SF Mono', 'Fira Code', monospace;
                    font-size: 0.72rem;
                }

                .rp-workspace-root i {
                    color: #38bdf8;
                    flex: 0 0 auto;
                }

                .rp-workspace-root span {
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }

                .rp-workspace-error {
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    margin: 10px 12px 0;
                    padding: 8px 10px;
                    border-radius: 6px;
                    background: rgba(239, 68, 68, 0.08);
                    border: 1px solid rgba(239, 68, 68, 0.16);
                    color: #fecaca;
                    font-size: 0.74rem;
                    line-height: 1.35;
                }

                .rp-workspace-main {
                    flex: 1;
                    display: grid;
                    grid-template-rows: minmax(145px, 36%) minmax(0, 1fr);
                    min-height: 0;
                    overflow: hidden;
                }

                .rp-workspace-tree {
                    overflow: auto;
                    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
                    padding: 8px;
                    min-height: 0;
                }

                .rp-workspace-entry-row {
                    display: flex;
                    align-items: center;
                    min-height: 30px;
                    margin-bottom: 2px;
                    border: 1px solid transparent;
                    border-radius: 6px;
                    transition: all 0.16s ease;
                }
                .rp-workspace-entry-row:hover {
                    background: rgba(255, 255, 255, 0.03);
                }
                .rp-workspace-entry-row.active {
                    color: #38bdf8;
                    background: rgba(56, 189, 248, 0.08);
                    border-color: rgba(56, 189, 248, 0.16);
                }
                .rp-workspace-expand-btn {
                    width: 26px;
                    min-height: 30px;
                    border: none;
                    background: transparent;
                    color: #64748b;
                    cursor: pointer;
                    flex: 0 0 auto;
                }
                .rp-workspace-expand-btn:hover { color: #e2e8f0; }
                .rp-workspace-expand-btn i { font-size: 0.66rem; }
                .rp-workspace-entry {
                    flex: 1;
                    min-width: 0;
                    min-height: 30px;
                    display: flex;
                    align-items: center;
                    gap: 8px;
                    border: none;
                    border-radius: 6px;
                    background: transparent;
                    color: #94a3b8;
                    cursor: pointer;
                    font-size: 0.76rem;
                    text-align: left;
                    padding: 6px 8px;
                }
                .rp-workspace-entry-row:hover .rp-workspace-entry { color: #e2e8f0; }
                .rp-workspace-entry-row.active .rp-workspace-entry { color: #38bdf8; }
                .rp-workspace-entry.directory i { color: #fbbf24; }
                .rp-workspace-entry.file i { color: #94a3b8; }
                .rp-workspace-selected-icon {
                    color: #34d399 !important;
                    font-size: 0.68rem;
                    margin-left: auto;
                }

                .rp-workspace-entry-name {
                    flex: 1;
                    min-width: 0;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }

                .rp-workspace-entry-size {
                    color: #64748b;
                    font-family: 'SF Mono', 'Fira Code', monospace;
                    font-size: 0.65rem;
                    flex: 0 0 auto;
                }

                .rp-workspace-preview {
                    min-height: 0;
                    display: flex;
                    flex-direction: column;
                    overflow: hidden;
                    background: rgba(2, 6, 23, 0.35);
                }

                .rp-workspace-preview-header {
                    min-height: 40px;
                    display: flex;
                    align-items: center;
                    justify-content: space-between;
                    gap: 10px;
                    padding: 8px 12px;
                    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
                    color: #e2e8f0;
                    font-family: 'SF Mono', 'Fira Code', monospace;
                    font-size: 0.73rem;
                }

                .rp-workspace-preview-header span {
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }

                .rp-workspace-preview-header a {
                    text-decoration: none;
                }

                .rp-workspace-text-preview {
                    flex: 1;
                    margin: 0;
                    padding: 12px;
                    overflow: auto;
                    color: #cbd5e1;
                    font-family: 'SF Mono', 'Fira Code', monospace;
                    font-size: 0.72rem;
                    line-height: 1.5;
                    white-space: pre-wrap;
                    word-break: break-word;
                }

                .rp-workspace-image-wrap {
                    flex: 1;
                    min-height: 0;
                    overflow: auto;
                    display: flex;
                    align-items: flex-start;
                    justify-content: center;
                    padding: 12px;
                }

                .rp-workspace-image-wrap img {
                    max-width: 100%;
                    height: auto;
                    border-radius: 6px;
                    border: 1px solid rgba(255, 255, 255, 0.08);
                    background: rgba(255, 255, 255, 0.04);
                }

                .rp-theme-root .rp-drawer,
                .rp-theme-root .rp-left-drawer {
                    background: var(--rp-panel-bg);
                    border-color: var(--rp-border);
                    color: var(--rp-text);
                }
                .rp-theme-root .rp-drawer-header,
                .rp-theme-root .rp-left-tabs,
                .rp-theme-root .rp-workspace-lock,
                .rp-theme-root .rp-workspace-toolbar,
                .rp-theme-root .rp-diff-content-header,
                .rp-theme-root .rp-console-cmd-header {
                    background: var(--rp-panel-strong-bg);
                    border-color: var(--rp-border);
                }
                .rp-theme-root .rp-session-card,
                .rp-theme-root .rp-call-card,
                .rp-theme-root .rp-console-item,
                .rp-theme-root .rp-todo-item,
                .rp-theme-root .rp-metric {
                    background: var(--rp-card-bg);
                    border-color: var(--rp-border);
                }
                .rp-theme-root .rp-todo-item.completed {
                    background: rgba(16, 185, 129, 0.03);
                    border-color: rgba(16, 185, 129, 0.08);
                }
                .rp-theme-root .rp-todo-item.status-pending {
                    background: rgba(245, 158, 11, 0.03);
                    border-color: rgba(245, 158, 11, 0.08);
                }
                .rp-theme-root .rp-todo-item.status-in_progress {
                    background: rgba(56, 189, 248, 0.03);
                    border-color: rgba(56, 189, 248, 0.1);
                }
                .rp-theme-root .rp-todo-item.status-cancelled {
                    background: rgba(249, 115, 22, 0.03);
                    border-color: rgba(249, 115, 22, 0.1);
                }
                .rp-theme-root .rp-drawer-title,
                .rp-theme-root .rp-session-value,
                .rp-theme-root .rp-todo-progress-label,
                .rp-theme-root .rp-diff-header-title,
                .rp-theme-root .rp-clarify-question,
                .rp-theme-root .rp-sudo-desc,
                .rp-theme-root .rp-workspace-root {
                    color: var(--rp-text);
                }
                .rp-theme-root .rp-drawer-subtitle,
                .rp-theme-root .rp-session-label,
                .rp-theme-root .rp-muted,
                .rp-theme-root .rp-empty-state,
                .rp-theme-root .rp-code-label,
                .rp-theme-root .rp-left-tab,
                .rp-theme-root .rp-diff-time {
                    color: var(--rp-muted-text);
                }
                .rp-theme-root .rp-tool-name,
                .rp-theme-root .rp-left-tab.active,
                .rp-theme-root .rp-workspace-entry-row.active,
                .rp-theme-root .rp-console-prompt,
                .rp-theme-root .rp-todo-progress-value {
                    color: var(--rp-accent);
                }
                .rp-theme-root .rp-left-tab.active {
                    border-bottom-color: var(--rp-accent);
                }
                .rp-theme-root .rp-model-select,
                .rp-theme-root .rp-code-block,
                .rp-theme-root .rp-console-output,
                .rp-theme-root .rp-workspace-preview,
                .rp-theme-root .rp-diff-scroller {
                    background: var(--rp-code-bg);
                    border-color: var(--rp-border);
                    color: var(--rp-text);
                }
                .rp-theme-root .rp-floating-btn,
                .rp-theme-root .rp-left-floating-btn {
                    background: linear-gradient(135deg, var(--rp-accent-strong), var(--rp-accent));
                }
                .rp-theme-root .rp-floating-btn:hover,
                .rp-theme-root .rp-left-floating-btn:hover {
                    background: linear-gradient(135deg, var(--rp-accent), var(--rp-accent-strong));
                }

                /* Mobile UI overrides inspired by CarrotKernel */
                @media (max-width: 1000px) {
                    /* Adjust bottom position to avoid ST's mobile bottom bar and increase z-index to stay on top */
                    .rp-floating-btn, .rp-left-floating-btn {
                        bottom: 75px !important;
                        width: 44px !important;
                        height: 44px !important;
                        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4) !important;
                        z-index: 9999 !important; /* High z-index for ST mobile, pointer-events: auto inherited from base */
                        pointer-events: auto !important;
                    }

                    .rp-left-floating-btn:not(.rp-has-custom-position) {
                        left: 10px !important;
                    }

                    .rp-floating-btn:not(.rp-has-custom-position) {
                        right: 10px !important;
                    }

                    /* Drawers must take full screen on mobile */
                    .rp-drawer, .rp-left-drawer {
                        width: 100vw !important;
                        max-width: 100vw !important;
                        top: 0 !important;
                        height: 100dvh !important;
                        z-index: 30001 !important;
                        border: none !important;
                    }

                    /* Adjust padding and font sizes for mobile readability */
                    .rp-drawer-header {
                        padding: 12px 15px !important;
                    }

                    .rp-drawer-title {
                        font-size: 1rem !important;
                    }

                    .rp-tool-call-item {
                        margin: 10px !important;
                        padding: 10px !important;
                    }

                    .rp-left-tab {
                        flex: 1 0 80px !important;
                        padding: 10px 4px !important;
                        font-size: 0.7rem !important;
                    }
                }
            `;
            document.head.appendChild(style);
    }, []);

    // Get current chat context & update chatId
    useEffect(() => {
        const updateChat = () => {
            const currentChatId = typeof context.getCurrentChatId === 'function' ? context.getCurrentChatId() : context.chatId;
            setChatId(currentChatId || null);
            const storedProfile = context.extensionSettings?.responsesProxy?.profileByChat?.[currentChatId];
            setSelectedProfile(typeof storedProfile === 'string' && storedProfile ? storedProfile : 'default');
            setAgentStatus({ active: false });
            setServerRequests([]);
            setServerRequestResetTokens({});
            setRightReadCallIds(new Set());
            setLeftReadCallIds(emptyLeftReadState());
            setToolCallsPage({ total: 0, limit: 100, offset: 0, has_more: false });
        };

        updateChat();

        context.eventSource.on(context.eventTypes.CHAT_CHANGED, updateChat);
        context.eventSource.on(context.eventTypes.CHAT_LOADED, updateChat);

        return () => {
            context.eventSource.removeListener(context.eventTypes.CHAT_CHANGED, updateChat);
            context.eventSource.removeListener(context.eventTypes.CHAT_LOADED, updateChat);
        };
    }, [context]);

    const saveSelectedModel = useCallback((model) => {
        const settings = context.extensionSettings.responsesProxy || {};
        context.extensionSettings.responsesProxy = {
            ...settings,
            selectedModel: model,
        };
        context.saveSettingsDebounced();
    }, [context]);

    const saveSelectedProfile = useCallback((profile) => {
        const settings = context.extensionSettings.responsesProxy || {};
        const currentChatId = chatIdRef.current;
        if (!currentChatId) return;
        context.extensionSettings.responsesProxy = {
            ...settings,
            profileByChat: {
                ...(settings.profileByChat || {}),
                [currentChatId]: profile,
            },
        };
        context.saveSettingsDebounced();
    }, [context]);

    const flattenModelOptions = useCallback((payload) => {
        const source = payload?.options || payload;
        const providers = source?.providers;
        if (!Array.isArray(providers)) return [];
        const models = [];
        const seen = new Set();
        providers.forEach((provider) => {
            const providerName = provider.name || provider.slug || 'Provider';
            const providerModels = Array.isArray(provider.models) ? provider.models : [];
            providerModels.forEach((model) => {
                if (typeof model !== 'string' || seen.has(model)) return;
                seen.add(model);
                models.push({ id: model, label: `${model} · ${providerName}` });
            });
        });
        return models;
    }, []);

    const showAgentIndicator = useCallback((status) => {
        const text = status?.text?.trim();
        const active = Boolean(status?.active && text);
        const existing = document.getElementById('responses_proxy_status_indicator');
        if (!active) {
            if (existing) {
                $(existing).hide(() => existing.remove());
            }
            return;
        }

        const setContent = (indicator) => {
            indicator.textContent = '';
            const dots = document.createElement('span');
            dots.className = 'rp-dots';
            dots.setAttribute('aria-hidden', 'true');
            dots.appendChild(document.createElement('span'));
            dots.appendChild(document.createElement('span'));
            dots.appendChild(document.createElement('span'));

            const label = document.createElement('span');
            label.textContent = text;

            indicator.appendChild(dots);
            indicator.appendChild(label);
        };

        if (existing) {
            setContent(existing);
            return;
        }

        const indicator = document.createElement('div');
        indicator.id = 'responses_proxy_status_indicator';
        indicator.classList.add('responses-proxy-status-indicator');
        setContent(indicator);
        $(indicator).hide();

        const chat = document.getElementById('chat');
        if (chat) {
            chat.appendChild(indicator);
            const wasChatScrolledDown = Math.ceil(chat.scrollTop + chat.clientHeight) >= chat.scrollHeight;
            $(indicator).show(() => {
                if (wasChatScrolledDown) {
                    chat.scrollTop = chat.scrollHeight;
                }
            });
        }
    }, []);

    useEffect(() => {
        showAgentIndicator(agentStatus);
        return () => showAgentIndicator({ active: false });
    }, [agentStatus, showAgentIndicator]);

    const requestSessionSnapshot = useCallback(() => {
        if (!chatId || !wsService.isConnected) return;
        wsService.send({ type: 'get_session_info', session_id: chatId });
    }, [chatId]);

    const requestModelOptions = useCallback(() => {
        if (!chatId || !wsService.isConnected) return;
        wsService.send({ type: 'get_model_options', session_id: chatId });
    }, [chatId]);

    const requestProfileOptions = useCallback(() => {
        if (!wsService.isConnected) return;
        wsService.send({ type: 'get_profile_options' });
    }, []);

    const deriveHttpBaseUrl = useCallback(() => {
        return proxyHttpBaseUrl(context);
    }, [context]);

    const handleModelChange = useCallback((event) => {
        const model = event.target.value;
        setSelectedModel(model);
        saveSelectedModel(model);

        if (chatId && model && wsService.isConnected) {
            wsService.send({ type: 'set_model', session_id: chatId, model });
        }
    }, [chatId, saveSelectedModel]);

    const handleProfileChange = useCallback((event) => {
        const profile = event.target.value || 'default';
        setSelectedProfile(profile);
        saveSelectedProfile(profile);
        if (chatId && wsService.isConnected) {
            wsService.send({ type: 'set_profile', session_id: chatId, profile });
            setSessionInfo(prev => ({ ...prev, profile, hermes_session_status: 'rebuilding' }));
        }
    }, [chatId, saveSelectedProfile]);

    const seedReadState = useCallback((calls) => {
        const safeCalls = Array.isArray(calls) ? calls : [];
        setRightReadCallIds(new Set(safeCalls.map(toolCallKey).filter(Boolean)));
        setLeftReadCallIds({
            console: new Set(leftTabToolCallIds(safeCalls, 'console')),
            diffs: new Set(leftTabToolCallIds(safeCalls, 'diffs')),
            todos: new Set(leftTabToolCallIds(safeCalls, 'todos')),
        });
    }, []);

    const fetchToolCallsPage = useCallback(async ({ offset = 0, append = false } = {}) => {
        if (!chatId) return;
        const baseUrl = deriveHttpBaseUrl();
        if (!baseUrl) return;

        const params = new URLSearchParams({
            limit: '100',
            offset: String(offset),
        });
        if (toolCallFilters.status) params.set('status', toolCallFilters.status);
        if (toolCallFilters.tool.trim()) params.set('tool', toolCallFilters.tool.trim());
        if (toolCallFilters.q.trim()) params.set('q', toolCallFilters.q.trim());

        setToolCallsLoading(true);
        try {
            const response = await proxyFetch(context, `${baseUrl}/v1/session/${encodeURIComponent(chatId)}/tool_calls?${params}`);
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.error?.message || data.detail || `Tool call history failed (${response.status})`);
            }
            const pageCalls = Array.isArray(data.tool_calls) ? [...data.tool_calls].reverse() : [];
            setToolCalls(prev => append ? [...pageCalls, ...prev] : pageCalls);
            setToolCallsPage({
                total: Number(data.total || 0),
                limit: Number(data.limit || 100),
                offset: Number(data.offset || 0),
                has_more: Boolean(data.has_more),
            });
            if (!append) seedReadState(pageCalls);
        } catch (error) {
            console.warn('[Hermes Bridge] Failed to fetch tool call history:', error);
        } finally {
            setToolCallsLoading(false);
        }
    }, [chatId, context, deriveHttpBaseUrl, seedReadState, toolCallFilters]);

    useEffect(() => {
        if (!chatId) return;
        fetchToolCallsPage({ offset: 0, append: false });
    }, [chatId, toolCallFilters, fetchToolCallsPage]);

    const markRightDrawerRead = useCallback(() => {
        const ids = toolCalls.map(toolCallKey).filter(Boolean);
        if (!ids.length) return;
        setRightReadCallIds(prev => {
            if (ids.every(id => prev.has(id))) return prev;
            const next = new Set(prev);
            ids.forEach(id => next.add(id));
            return next;
        });
    }, [toolCalls]);

    const markLeftTabRead = useCallback((tab) => {
        const ids = leftTabToolCallIds(toolCalls, tab);
        if (!ids.length) return;
        setLeftReadCallIds(prev => {
            const existing = prev[tab] || new Set();
            if (ids.every(id => existing.has(id))) return prev;
            const nextSet = new Set(existing);
            ids.forEach(id => nextSet.add(id));
            return { ...prev, [tab]: nextSet };
        });
    }, [toolCalls]);

    useEffect(() => {
        if (isOpen) {
            markRightDrawerRead();
        }
    }, [isOpen, markRightDrawerRead]);

    useEffect(() => {
        if (isLeftOpen) {
            markLeftTabRead(activeLeftTab);
        }
    }, [isLeftOpen, activeLeftTab, markLeftTabRead]);

    useEffect(() => {
        const handleSettingsUpdated = (event) => {
            setExtensionSettings({
                ...normalizedExtensionSettings(context),
                ...(event.detail || {}),
                browserNotifications: {
                    ...DEFAULT_NOTIFICATION_SETTINGS,
                    ...(event.detail?.browserNotifications || context.extensionSettings?.responsesProxy?.browserNotifications || {}),
                },
            });
        };
        window.addEventListener('responses-proxy-settings-updated', handleSettingsUpdated);
        return () => window.removeEventListener('responses-proxy-settings-updated', handleSettingsUpdated);
    }, [context]);

    useEffect(() => {
        if (!agentControlMessage) return undefined;
        const timer = setTimeout(() => setAgentControlMessage(null), 5000);
        return () => clearTimeout(timer);
    }, [agentControlMessage]);

    const notifyBrowser = useCallback((kind, title, body, tag) => {
        const notificationSettings = extensionSettingsRef.current.browserNotifications || DEFAULT_NOTIFICATION_SETTINGS;
        if (notificationSettings[kind] === false) return;
        if (!('Notification' in window)) return;

        const show = () => {
            try {
                const notification = new Notification(title, {
                    body,
                    tag,
                    silent: false,
                });
                notification.onclick = () => {
                    window.focus();
                    setIsOpen(true);
                    notification.close();
                };
            } catch (e) {
                console.warn('[Hermes Bridge] Browser notification failed:', e);
            }
        };

        if (Notification.permission === 'granted') {
            show();
        } else if (Notification.permission === 'default') {
            Notification.requestPermission().then((permission) => {
                if (permission === 'granted') show();
            }).catch((error) => {
                console.warn('[Hermes Bridge] Notification permission request failed:', error);
            });
        }
    }, []);

    // ─── WebSocket lifecycle ────────────────────────────────────────

    // These callbacks are dependencies of the WebSocket listener effect below,
    // so initialize them before React evaluates its dependency array.
    const pulseButton = useCallback(() => {
        const btn = document.querySelector('.rp-floating-btn');
        if (btn) {
            btn.classList.remove('pulse');
            void btn.offsetWidth;
            btn.classList.add('pulse');
        }
    }, []);

    const pulseLeftButton = useCallback(() => {
        const btn = document.querySelector('.rp-left-floating-btn');
        if (btn) {
            btn.classList.remove('pulse');
            void btn.offsetWidth;
            btn.classList.add('pulse');
        }
    }, []);

    const scrollToLatest = useCallback(() => {
        if (!panelRef.current) return;
        const bodyEl = panelRef.current.querySelector('.rp-drawer-body');
        if (!bodyEl) return;
        const newestCard = bodyEl.querySelector('.rp-call-card.newest');
        if (newestCard) {
            newestCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
    }, []);

    useEffect(() => {
        const isEnabled = context.extensionSettings?.responsesProxy?.isEnabled ?? true;
        if (!isEnabled) {
            wsService.disconnect();
            setWsStatus('disconnected');
            setToolCalls([]);
            setToolCallsPage({ total: 0, limit: 100, offset: 0, has_more: false });
            setSessionInfo({});
            setAgentStatus({ active: false });
            setServerRequests([]);
            prevWsUrlRef.current = null;
            return undefined;
        }

        // Set up event listeners before connecting
        const unsubConnected = wsService.on('connected', () => {
            setWsStatus('connected');
            requestSessionSnapshot();
            requestModelOptions();
            requestProfileOptions();
        });

        const unsubSubscribed = wsService.on('subscribed', (data) => {
            // Initial tool calls snapshot from server (limited to 100 most recent)
            const snapshotCalls = (data.tool_calls || []).slice(-100);
            setToolCalls(snapshotCalls);
            seedReadState(snapshotCalls);
            setServerRequests(normalizeServerRequestSnapshot(data.server_requests));
            setServerRequestResetTokens({});
            if (data.session_info) {
                setSessionInfo(data.session_info);
                if (data.session_info.agent_status) {
                    setAgentStatus(data.session_info.agent_status);
                }
            }
        });

        const unsubServerRequests = wsService.on('server_requests', (requests) => {
            setServerRequests(normalizeServerRequestSnapshot(requests));
        });

        const unsubServerRequest = wsService.on('server_request', (data) => {
            if (data.session_id && data.session_id !== chatIdRef.current) return;
            setServerRequests((previous) => upsertServerRequest(previous, data));
            const method = data.method;
            const params = data.params || {};
            const summary = method === 'sudo'
                ? 'Hermes is waiting for a sudo password.'
                : String(params.description || params.question || 'The agent is waiting for your input.').slice(0, 180);
            notifyBrowser(
                method,
                method === 'approval' ? 'Approval needed' : method === 'clarify' ? 'Clarification needed' : 'Sudo password needed',
                summary,
                `responses-proxy-${method}-${data.rpc_id || 'current'}`,
            );
            pulseButton();
            setIsOpen(true);
        });

        const removeInteractiveRequest = (data) => {
            if (data.session_id && data.session_id !== chatIdRef.current) return;
            setServerRequests((previous) => removeServerRequest(previous, data));
            if (data.rpc_id) {
                setServerRequestResetTokens((previous) => {
                    if (!(data.rpc_id in previous)) return previous;
                    const next = { ...previous };
                    delete next[data.rpc_id];
                    return next;
                });
            }
        };
        const unsubServerRequestCancel = wsService.on('server_request_cancel', removeInteractiveRequest);
        const unsubServerRequestResolved = wsService.on('server_request_resolved', removeInteractiveRequest);

        const unsubServerRequestError = wsService.on('server_request_error', (data) => {
            if (data.session_id && data.session_id !== chatIdRef.current) return;
            const action = classifyServerRequestError(data);
            if (action === 'retry') {
                setServerRequestResetTokens((previous) => ({
                    ...previous,
                    [data.rpc_id]: (previous[data.rpc_id] || 0) + 1,
                }));
                return;
            }
            if (action === 'remove') removeInteractiveRequest(data);
        });

        const unsubServerRequestLockAck = wsService.on('server_request_clarify_lock_ack', (data) => {
            if (data.session_id && data.session_id !== chatIdRef.current) return;
            if (!data.rpc_id) return;
            setServerRequestResetTokens((previous) => ({
                ...previous,
                [data.rpc_id]: (previous[data.rpc_id] || 0) + 1,
            }));
        });

        const unsubToolCalls = wsService.on('tool_calls', (calls) => {
            const limited = (calls || []).slice(-100);
            setToolCalls(limited);
        });

        let pendingUpdates = [];
        let updateTimeout = null;

        const unsubToolCallEvent = wsService.on('tool_call_event', (data) => {
            if (!data.tool_call) return;
            pendingUpdates.push(data.tool_call);

            if (isToolError(data.tool_call)) {
                const key = data.tool_call.id || toolCallKey(data.tool_call);
                if (key && !notifiedToolErrorsRef.current.has(key)) {
                    notifiedToolErrorsRef.current.add(key);
                    notifyBrowser(
                        'toolError',
                        `Tool failed: ${data.tool_call.name || 'unknown'}`,
                        String(data.tool_call.error || data.tool_call.stderr || data.tool_call.output || 'The tool returned an error.').slice(0, 180),
                        `responses-proxy-tool-error-${key}`,
                    );
                }
            }

            if (!updateTimeout) {
                updateTimeout = setTimeout(() => {
                    updateTimeout = null;
                    const updates = pendingUpdates;
                    pendingUpdates = [];
                    setToolCalls(prev => {
                        let updated = [...prev];
                        for (const call of updates) {
                            if (!call) continue;
                            const idx = updated.findIndex(c => c.id === call.id);
                            if (idx >= 0) {
                                updated[idx] = { ...updated[idx], ...call };
                            } else {
                                updated.push(call);
                            }
                        }
                        return updated.slice(-100);
                    });
                }, 100);
            }

            // Throttle expensive DOM operations
            const throttledPulse = throttle(() => {
                pulseButton();
                pulseLeftButton();
            }, 120);
            throttledPulse();

            // Auto-scroll if drawer is open (throttled)
            if (isOpen) {
                const throttledScroll = throttle(() => {
                    requestAnimationFrame(() => {
                        scrollToLatest();
                    });
                }, 150);
                throttledScroll();
            }
        });

        const unsubCleared = wsService.on('tool_calls_cleared', (data) => {
            if (data?.session_id && data.session_id !== chatIdRef.current) return;
            if (updateTimeout) {
                clearTimeout(updateTimeout);
                updateTimeout = null;
            }
            pendingUpdates = [];
            setToolCalls([]);
            setToolCallsPage({ total: 0, limit: 100, offset: 0, has_more: false });
            setRightReadCallIds(new Set());
            setLeftReadCallIds(emptyLeftReadState());
        });

        const unsubSessionInfo = wsService.on('session_info', (data) => {
            if (data.session_id && data.session_id !== chatIdRef.current) return;
            setSessionInfo(prev => ({ ...prev, ...data }));
            if (data.agent_status) {
                setAgentStatus(data.agent_status);
            }
        });

        const unsubAgentStatus = wsService.on('agent_status', (data) => {
            if (data.session_id && data.session_id !== chatIdRef.current) return;
            setAgentStatus(data.status || { active: false });
        });

        const unsubModelOptions = wsService.on('model_options', (data) => {
            setModelOptions(flattenModelOptions(data));
        });

        const unsubProfileOptions = wsService.on('profile_options', (data) => {
            const source = data?.options || data;
            setProfileOptions(Array.isArray(source?.profiles) ? source.profiles : []);
        });

        const unsubProfileChanged = wsService.on('profile_changed', (data) => {
            if (data.session_id && data.session_id !== chatIdRef.current) return;
            if (data.profile) {
                setSelectedProfile(data.profile);
                saveSelectedProfile(data.profile);
                setSessionInfo(prev => ({ ...prev, profile: data.profile, hermes_session_status: 'rebuilding' }));
            }
        });

        const unsubModelChanged = wsService.on('model_changed', (data) => {
            if (data.session_id && data.session_id !== chatIdRef.current) return;
            const model = data.model || data.session_info?.model || '';
            if (model) {
                setSelectedModel(model);
                saveSelectedModel(model);
            }
            if (data.session_info) {
                setSessionInfo(data.session_info);
            } else if (model) {
                setSessionInfo(prev => ({ ...prev, model }));
            }
        });

        const unsubSessionDeleted = wsService.on('session_deleted', (data) => {
            if (data.session_id && data.session_id !== chatIdRef.current) return;
            setToolCalls([]);
            setToolCallsPage({ total: 0, limit: 100, offset: 0, has_more: false });
            setSessionInfo({});
            setAgentStatus({ active: false });
            setServerRequests([]);
            setServerRequestResetTokens({});
            setRightReadCallIds(new Set());
            setLeftReadCallIds(emptyLeftReadState());
        });

        const unsubUsageUpdate = wsService.on('usage_update', (data) => {
            if (data.session_id && data.session_id !== chatIdRef.current) return;
            if (data.streaming) {
                setStreamingEstimate({
                    outputTokens: data.estimated_output_tokens || 0,
                    chars: data.estimated_output_chars || 0,
                });
            } else {
                setStreamingEstimate(null);
            }
        });

        const unsubServerError = wsService.on('server_error', (data) => {
            console.warn('[Hermes Bridge WS] Server command error:', data.message || data);
        });

        const unsubAgentControlResult = wsService.on('agent_control_result', (data) => {
            if (data.session_id && data.session_id !== chatIdRef.current) return;
            const action = data.action || 'action';
            const waiter = data.request_id ? agentControlWaitersRef.current.get(data.request_id) : null;
            if (waiter) {
                clearTimeout(waiter.timeoutId);
                agentControlWaitersRef.current.delete(data.request_id);
            }
            const busyAction = waiter?.busyAction || action;
            setAgentControlBusy(prev => ({ ...prev, [busyAction]: false, [action]: false }));
            const isError = data.status === 'error';
            const text = agentControlResultMessage(data);
            const shouldNotify = !waiter?.silent || isError;
            if (shouldNotify) {
                setAgentControlMessage({ tone: isError ? 'error' : 'ok', text });
            }
            if (isError) {
                console.warn('[Hermes Bridge WS] Agent control failed:', data);
                SillyTavern.toastr?.error?.(text);
                waiter?.reject?.(new Error(text));
                return;
            }
            if (shouldNotify) {
                SillyTavern.toastr?.success?.(text);
            }
            if (data.session_info) {
                setSessionInfo(data.session_info);
            }
            waiter?.resolve?.(data);
            requestSessionSnapshot();
        });

        const unsubDisconnected = wsService.on('disconnected', () => {
            setWsStatus('disconnected');
            setServerRequests([]);
            setServerRequestResetTokens({});
            setAgentControlBusy({});
            agentControlWaitersRef.current.forEach((waiter) => {
                clearTimeout(waiter.timeoutId);
                waiter.reject?.(new Error('WebSocket disconnected'));
            });
            agentControlWaitersRef.current.clear();
        });

        const unsubError = wsService.on('error', () => {
            setWsStatus('disconnected');
            setServerRequests([]);
            setServerRequestResetTokens({});
            setAgentControlBusy({});
            agentControlWaitersRef.current.forEach((waiter) => {
                clearTimeout(waiter.timeoutId);
                waiter.reject?.(new Error('WebSocket error'));
            });
            agentControlWaitersRef.current.clear();
        });

        return () => {
            if (updateTimeout) clearTimeout(updateTimeout);
            unsubConnected();
            unsubSubscribed();
            unsubServerRequests();
            unsubServerRequest();
            unsubServerRequestCancel();
            unsubServerRequestResolved();
            unsubServerRequestError();
            unsubServerRequestLockAck();
            unsubToolCalls();
            unsubToolCallEvent();
            unsubCleared();
            unsubSessionInfo();
            unsubAgentStatus();
            unsubModelOptions();
            unsubModelChanged();
            unsubProfileOptions();
            unsubProfileChanged();
            unsubSessionDeleted();
            unsubUsageUpdate();
            unsubServerError();
            unsubAgentControlResult();
            unsubDisconnected();
            unsubError();
            agentControlWaitersRef.current.forEach((waiter) => {
                clearTimeout(waiter.timeoutId);
                waiter.reject?.(new Error('Agent control listener was removed'));
            });
            agentControlWaitersRef.current.clear();
        };
    }, [context, notifyBrowser, pulseButton, pulseLeftButton, requestModelOptions, requestProfileOptions, requestSessionSnapshot, saveSelectedModel, saveSelectedProfile, scrollToLatest, seedReadState]);  // eslint-disable-line react-hooks/exhaustive-deps

    useEffect(() => {
        const isEnabled = context.extensionSettings?.responsesProxy?.isEnabled ?? true;
        if (!isEnabled) return;
        wsService.setContext(context);
        const url = wsService.constructor.deriveUrl(context);
        prevWsUrlRef.current = url;
        setWsStatus(wsService.isConnected ? 'connected' : 'connecting');
        wsService.connect(null, url);
        return () => {
            wsService.disconnect();
            prevWsUrlRef.current = null;
        };
    }, [context]);

    // Update WS subscription when chatId changes without reconnecting the socket.
    useEffect(() => {
        const isEnabled = context.extensionSettings?.responsesProxy?.isEnabled ?? true;
        if (!isEnabled) return;
        wsService.subscribe(chatId || null);
        if (!chatId) {
            // Keep the global socket alive on SillyTavern's home screen, but
            // clear state that belongs to the previously selected session.
            setToolCalls([]);
            setToolCallsPage({ total: 0, limit: 100, offset: 0, has_more: false });
            setSessionInfo({});
            setAgentStatus({ active: false });
            setServerRequests([]);
            setServerRequestResetTokens({});
            return;
        }
        if (wsService.isConnected && chatId) {
            requestSessionSnapshot();
            requestModelOptions();
            requestProfileOptions();
        }
    }, [chatId, context, requestModelOptions, requestProfileOptions, requestSessionSnapshot]);

    // Refresh context usage while the panel is mounted. Hermes updates usage at
    // turn boundaries, so a low-frequency poll is enough and keeps the UI fresh.
    useEffect(() => {
        if (!chatId) return undefined;
        requestSessionSnapshot();
        const interval = setInterval(requestSessionSnapshot, 15000);
        return () => clearInterval(interval);
    }, [chatId, requestSessionSnapshot]);

    // Clear local drawer state when ST deletes a chat file. Remote cleanup is
    // handled by the extension bootstrap so it also runs when the drawer is shut.
    useEffect(() => {
        const onDeleted = (deletedChatId) => {
            if (!deletedChatId) return;
            const deleted = String(deletedChatId).replace(/\.jsonl$/i, '');
            if (deleted === chatId) {
                setToolCalls([]);
                setSessionInfo({});
                setAgentStatus({ active: false });
                setServerRequests([]);
                setServerRequestResetTokens({});
                setRightReadCallIds(new Set());
                setLeftReadCallIds(emptyLeftReadState());
            }
        };

        const events = [
            context.eventTypes.CHAT_DELETED,
            context.eventTypes.GROUP_CHAT_DELETED,
        ].filter(Boolean);

        events.forEach(eventType => context.eventSource.on(eventType, onDeleted));
        return () => {
            events.forEach(eventType => context.eventSource.removeListener(eventType, onDeleted));
        };
    }, [chatId, context]);

    // Keep-alive ping
    useEffect(() => {
        const interval = setInterval(() => {
            if (wsService.isConnected) {
                wsService.send({ type: 'ping' });
            }
        }, 30000);
        return () => clearInterval(interval);
    }, []);

    // ─── Helpers ────────────────────────────────────────────────────

    const handleClearCalls = async () => {
        if (!chatId) return;
        wsService.send({ type: 'clear_tool_calls', session_id: chatId });
        setToolCalls([]);
        setToolCallsPage({ total: 0, limit: 100, offset: 0, has_more: false });
        setRightReadCallIds(new Set());
        setLeftReadCallIds(emptyLeftReadState());
    };

    const handleToolCallFilterChange = (key) => (event) => {
        setToolCallFilters(prev => ({
            ...prev,
            [key]: event.target.value,
        }));
    };

    const handleLoadOlderToolCalls = () => {
        if (toolCallsLoading || !toolCallsPage.has_more) return;
        fetchToolCallsPage({
            offset: toolCallsPage.offset + toolCallsPage.limit,
            append: true,
        });
    };

    const handleClarifySubmit = (rpcId, answer, questionId) => {
        if (!chatId || !rpcId) return;
        const clarify = pendingClarifies.find((item) => item.rpc_id === rpcId);
        if (!clarify) return;
        if (clarify.questions?.length) {
            wsService.send(buildClarifyLock(chatId, clarify, questionId, answer));
        } else {
            wsService.send(buildServerRequestResponse(chatId, clarify, { answer }));
        }
    };

    const handleApprovalSubmit = (rpcId, choice) => {
        if (!chatId || !rpcId) return;
        const approval = pendingApprovals.find((item) => item.rpc_id === rpcId);
        if (!approval) return;
        wsService.send(buildServerRequestResponse(chatId, approval, { choice, all: false }));
    };

    const handleSudoSubmit = (rpcId, password) => {
        if (!chatId || !rpcId) return;
        const sudo = pendingSudos.find((item) => item.rpc_id === rpcId);
        if (!sudo) return;
        wsService.send(buildServerRequestResponse(chatId, sudo, { value: password || '' }));
    };

    const sendAgentControlRequest = useCallback((action, payload = {}, options = {}) => {
        if (!chatId || !wsService.isConnected) {
            const error = new Error('WebSocket is not connected');
            setAgentControlMessage({ tone: 'error', text: error.message });
            return Promise.reject(error);
        }
        const requestId = `agent-control-${Date.now()}-${++_agentControlRequestCounter}`;
        const busyAction = options.busyAction || action;
        setAgentControlBusy(prev => ({ ...prev, [busyAction]: true }));
        setAgentControlMessage(null);

        return new Promise((resolve, reject) => {
            const timeoutId = setTimeout(() => {
                agentControlWaitersRef.current.delete(requestId);
                setAgentControlBusy(prev => ({ ...prev, [busyAction]: false }));
                reject(new Error(`${agentControlLabel(busyAction)} timed out`));
            }, options.timeoutMs || 30000);

            agentControlWaitersRef.current.set(requestId, {
                resolve,
                reject,
                timeoutId,
                busyAction,
                silent: Boolean(options.silent),
            });

            wsService.send({
                type: 'agent_control',
                session_id: chatId,
                action,
                request_id: requestId,
                ...payload,
            });
        });
    }, [chatId]);

    const sendAgentControl = useCallback((action, payload = {}) => {
        sendAgentControlRequest(action, payload).catch((error) => {
            console.warn('[Hermes Bridge] Agent control failed:', error);
        });
    }, [sendAgentControlRequest]);

    const handleInterruptAgent = useCallback(() => {
        sendAgentControl('interrupt');
    }, [sendAgentControl]);

    const handleSteerAgent = useCallback(() => {
        const text = window.prompt('Steer Hermes after the next tool call:');
        if (text === null) return;
        const trimmed = text.trim();
        if (!trimmed) return;
        sendAgentControl('steer', { text: trimmed });
    }, [sendAgentControl]);

    const handleUndoAgent = useCallback(async () => {
        const range = lastUserTurnRange(context.chat);
        if (!range) {
            const text = 'No user turn to undo';
            setAgentControlMessage({ tone: 'error', text });
            SillyTavern.toastr?.warning?.(text);
            return;
        }

        const confirmed = window.confirm(
            `Undo the last user turn?\n\nThis removes ${range.count} visible message${range.count === 1 ? '' : 's'} from SillyTavern and rewinds Hermes context. File changes and tool activity history stay visible.`,
        );
        if (!confirmed) return;

        try {
            setAgentControlBusy(prev => ({ ...prev, undo: true }));
            await sendAgentControlRequest('undo', {}, { busyAction: 'undo', silent: true });
            setAgentControlBusy(prev => ({ ...prev, undo: true }));
            const deleted = await deleteVisibleMessageRange(context, range.start, range.end);
            const text = `Undid last user turn; removed ${deleted} visible message${deleted === 1 ? '' : 's'}. Tool activity history kept.`;
            setAgentControlMessage({ tone: 'ok', text });
            SillyTavern.toastr?.success?.(text);
            requestSessionSnapshot();
        } catch (error) {
            const text = error?.message || 'Undo failed';
            console.warn('[Hermes Bridge] Undo failed:', error);
            setAgentControlMessage({ tone: 'error', text });
            SillyTavern.toastr?.error?.(text);
        } finally {
            setAgentControlBusy(prev => ({ ...prev, undo: false }));
        }
    }, [context, requestSessionSnapshot, sendAgentControlRequest]);

    const handleRetryAgent = useCallback(async () => {
        const range = lastUserTurnRange(context.chat);
        if (!range) {
            const text = 'No user turn to retry';
            setAgentControlMessage({ tone: 'error', text });
            SillyTavern.toastr?.warning?.(text);
            return;
        }
        if (typeof context.generate !== 'function') {
            const text = 'SillyTavern generation API is unavailable';
            setAgentControlMessage({ tone: 'error', text });
            SillyTavern.toastr?.error?.(text);
            return;
        }

        const confirmed = window.confirm(
            'Retry the last reply?\n\nThis keeps the last user message and lets SillyTavern regenerate the assistant response. Hermes will rewind automatically before the request is submitted. File changes and previous tool activity stay visible.',
        );
        if (!confirmed) return;

        try {
            setAgentControlBusy(prev => ({ ...prev, retry: true }));
            setAgentControlMessage({ tone: 'ok', text: 'Retrying last reply in SillyTavern...' });
            await context.generate('regenerate');
            const text = 'Retry completed';
            setAgentControlMessage({ tone: 'ok', text });
            SillyTavern.toastr?.success?.(text);
            requestSessionSnapshot();
        } catch (error) {
            const text = error?.message || 'Retry failed';
            console.warn('[Hermes Bridge] Retry failed:', error);
            setAgentControlMessage({ tone: 'error', text });
            SillyTavern.toastr?.error?.(text);
        } finally {
            setAgentControlBusy(prev => ({ ...prev, retry: false }));
        }
    }, [context, requestSessionSnapshot]);

    const handleCompressAgent = useCallback(() => {
        const focusTopic = window.prompt('Optional compression focus topic:', '');
        if (focusTopic === null) return;
        sendAgentControl('compress', { focus_topic: focusTopic.trim() });
    }, [sendAgentControl]);

    const formatTimestamp = (isoString) => {
        try {
            const date = new Date(isoString);
            return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        } catch (e) {
            return '';
        }
    };

    const formatTokenCount = (value) => {
        const number = Number(value);
        if (!Number.isFinite(number)) return '—';
        return Math.round(number).toLocaleString();
    };

    const usage = sessionInfo.last_usage || sessionInfo.usage || {};
    const contextMax = Number(usage.context_max || usage.context_window || usage.max_context_tokens || 0);
    const contextUsed = Number(usage.context_used || usage.total || usage.total_tokens || 0);
    const contextRemaining = Number.isFinite(Number(usage.context_remaining))
        ? Number(usage.context_remaining)
        : (contextMax && contextUsed ? Math.max(0, contextMax - contextUsed) : 0);
    const contextPercent = Number.isFinite(Number(usage.context_percent))
        ? Number(usage.context_percent)
        : (contextMax && contextUsed ? Math.min(100, Math.round((contextUsed / contextMax) * 100)) : 0);
    const inputTokens = usage.input ?? usage.prompt_tokens ?? usage.input_tokens;
    const outputTokens = usage.output ?? usage.completion_tokens ?? usage.output_tokens;
    const reasoningTokens = usage.reasoning ?? usage.reasoning_tokens ?? 0;
    const cacheReadTokens = usage.cache_read ?? usage.cache_read_tokens ?? 0;
    const cacheWriteTokens = usage.cache_write ?? usage.cache_write_tokens ?? 0;
    const costUsd = usage.cost_usd ?? 0;
    const compressions = usage.compressions ?? 0;
    const usageTone = contextPercent >= 90 ? 'danger' : contextPercent >= 75 ? 'warn' : '';
    const currentModel = sessionInfo.model || sessionInfo.hermes?.model || '';
    const currentProfile = sessionInfo.profile || sessionInfo.hermes?.profile_name || sessionInfo.hermes?.profile || selectedProfile || 'default';

    // Streaming estimate: merge real-time output token estimate
    const isStreaming = streamingEstimate != null && streamingEstimate.outputTokens > 0;
    const estimatedOutputTokens = isStreaming
        ? (Number(outputTokens) || 0) + streamingEstimate.outputTokens
        : outputTokens;
    // Overflow prediction: estimate turns remaining based on average turn size
    const avgTurnTokens = Number(outputTokens) > 0
        ? Math.max(1, Math.round((Number(inputTokens) + Number(outputTokens)) / Math.max(1, (sessionInfo.total_requests || 1))))
        : 4096;
    const turnsRemaining = contextMax > 0 && avgTurnTokens > 0
        ? Math.max(0, Math.floor(contextRemaining / avgTurnTokens))
        : 0;

    // Memoized expensive render calculations
    const selectedModelOptions = useMemo(() => {
        const options = [...modelOptions];
        if (selectedModel && !options.some(model => model.id === selectedModel)) {
            options.unshift({ id: selectedModel, label: selectedModel });
        }
        return options;
    }, [modelOptions, selectedModel]);

    const statusText = useMemo(() => agentStatus?.text?.trim(), [agentStatus]);
    const isAgentRunning = Boolean(agentStatus?.active);
    const reportedHermesSessionStatus = sessionInfo.hermes_session_status || 'not_started';
    const hermesSessionStatus = useMemo(() => {
        if (wsStatus !== 'connected') return 'unavailable';
        if (!isAgentRunning) return reportedHermesSessionStatus;
        return ['expired', 'not_started', 'rebuilding'].includes(reportedHermesSessionStatus)
            ? 'rebuilding'
            : 'working';
    }, [isAgentRunning, reportedHermesSessionStatus, wsStatus]);
    const hermesSessionPresentation = HERMES_SESSION_STATUS[hermesSessionStatus] || HERMES_SESSION_STATUS.unavailable;
    const canUseAgentControls = Boolean(chatId && wsStatus === 'connected');
    const canControlRunningAgent = canUseAgentControls && isAgentRunning;
    const canControlIdleAgent = canUseAgentControls && !isAgentRunning;

    const isExtensionEnabled = context.extensionSettings?.responsesProxy?.isEnabled ?? true;
    if (!isExtensionEnabled) {
        return null;
    }

    const visibleToolCalls = useMemo(() => {
        const status = toolCallFilters.status;
        const tool = toolCallFilters.tool.trim().toLowerCase();
        const q = toolCallFilters.q.trim().toLowerCase();
        return toolCalls.filter((call) => {
            if (status && call.status !== status) return false;
            if (tool && !String(call.name || '').toLowerCase().includes(tool)) return false;
            if (q) {
                const haystack = ['name', 'arguments', 'output', 'result_text', 'stdout', 'stderr', 'error']
                    .map(key => String(call[key] || ''))
                    .join(' ')
                    .toLowerCase();
                if (!haystack.includes(q)) return false;
            }
            return true;
        });
    }, [toolCalls, toolCallFilters]);

    const reversedCalls = useMemo(() => [...visibleToolCalls].reverse(), [visibleToolCalls]);

    const wsStatusColor = useMemo(() => {
        if (wsStatus === 'connected') return 'connected';
        if (wsStatus === 'connecting') return 'connecting';
        return 'disconnected';
    }, [wsStatus]);

    const hasPendingAction = useMemo(
        () => pendingClarifies.length > 0 || pendingApprovals.length > 0 || pendingSudos.length > 0,
        [pendingClarifies, pendingApprovals, pendingSudos]
    );
    const hasToolCallFilters = Boolean(toolCallFilters.status || toolCallFilters.tool.trim() || toolCallFilters.q.trim());

    const toolCallIds = useMemo(
        () => toolCalls.map(toolCallKey).filter(Boolean),
        [toolCalls]
    );

    const rightBadgeCount = useMemo(
        () => unreadCount(toolCallIds, rightReadCallIds),
        [toolCallIds, rightReadCallIds]
    );

    const consoleBadgeCount = useMemo(
        () => unreadCount(leftTabToolCallIds(toolCalls, 'console'), leftReadCallIds.console),
        [toolCalls, leftReadCallIds.console]
    );

    const diffBadgeCount = useMemo(
        () => unreadCount(leftTabToolCallIds(toolCalls, 'diffs'), leftReadCallIds.diffs),
        [toolCalls, leftReadCallIds.diffs]
    );

    const todoBadgeCount = useMemo(
        () => unreadCount(leftTabToolCallIds(toolCalls, 'todos'), leftReadCallIds.todos),
        [toolCalls, leftReadCallIds.todos]
    );

    const leftBadgeCount = useMemo(
        () => consoleBadgeCount + diffBadgeCount + todoBadgeCount,
        [consoleBadgeCount, diffBadgeCount, todoBadgeCount]
    );

    const httpBaseUrl = useMemo(deriveHttpBaseUrl, [deriveHttpBaseUrl]);
    const panelThemeMode = extensionSettings.themeMode || 'system';
    const focusWorkspacePath = useCallback((path) => {
        focusPath(path);
        setIsLeftOpen(true);
        setActiveLeftTab('workspace');
    }, [focusPath]);

    return (
        <div className="rp-theme-root" data-rp-theme={panelThemeMode}>
            <VoiceCallTrigger active={voiceActive} open={voiceOpen} onActivate={onOpenVoice} />

            {/* Left Floating button */}
            {!isLeftOpen && (
                <button
                    type="button"
                    {...leftFloatingButton}
                    className={`rp-left-floating-btn ${leftFloatingButton.className}`}
                    title="Open Workspace Console & Plan (drag to move)"
                    aria-label="Open Workspace Console & Plan"
                >
                    <i className="fa-solid fa-bars-progress"></i>
                    <NotificationBadge count={leftBadgeCount} />
                </button>
            )}

            {/* Left Side Drawer Panel */}
            <div className="rp-left-drawer" style={{ transform: isLeftOpen ? 'translateX(0)' : 'translateX(-100%)' }}>
                <div className="rp-drawer-header">
                    <div className="rp-drawer-title-container">
                        <h4 className="rp-drawer-title">
                            <i className="fa-solid fa-folder-open" style={{ color: '#00b4d8' }}></i>
                            Workspace Console & Plan
                        </h4>
                        <div className="rp-session-heading-row">
                            <div className="rp-drawer-subtitle" title={chatId || 'No active chat'}>
                                Session: {chatId || 'None'}
                            </div>
                            <span className={`rp-hermes-session-badge status-${hermesSessionStatus}`} title={hermesSessionPresentation.title}>
                                <i className={`fa-solid ${hermesSessionPresentation.icon}`}></i>
                                <span>{hermesSessionPresentation.label}</span>
                            </span>
                        </div>
                    </div>
                    <button className="rp-close-btn" onClick={() => setIsLeftOpen(false)}>
                        <i className="fa-solid fa-xmark"></i>
                    </button>
                </div>

                <div className="rp-left-tabs">
                    <button className={`rp-left-tab ${activeLeftTab === 'console' ? 'active' : ''}`} onClick={() => { setActiveLeftTab('console'); markLeftTabRead('console'); }}>
                        <i className="fa-solid fa-terminal"></i>
                        Console
                        <NotificationBadge count={consoleBadgeCount} />
                    </button>
                    <button className={`rp-left-tab ${activeLeftTab === 'diffs' ? 'active' : ''}`} onClick={() => { setActiveLeftTab('diffs'); markLeftTabRead('diffs'); }}>
                        <i className="fa-solid fa-code-compare"></i>
                        Code Diffs
                        <NotificationBadge count={diffBadgeCount} />
                    </button>
                    <button className={`rp-left-tab ${activeLeftTab === 'todos' ? 'active' : ''}`} onClick={() => { setActiveLeftTab('todos'); markLeftTabRead('todos'); }}>
                        <i className="fa-solid fa-list-check"></i>
                        Plan Todos
                        <NotificationBadge count={todoBadgeCount} />
                    </button>
                    <button className={`rp-left-tab ${activeLeftTab === 'workspace' ? 'active' : ''}`} onClick={() => setActiveLeftTab('workspace')}>
                        <i className="fa-solid fa-folder-tree"></i>
                        Workspace
                    </button>
                </div>

                <div style={{ flex: 1, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
                    {activeLeftTab === 'console' && <TerminalConsole toolCalls={toolCalls} />}
                    {activeLeftTab === 'diffs' && <DiffsTab toolCalls={toolCalls} focusPath={focusWorkspacePath} />}
                    {activeLeftTab === 'todos' && <TodoChecklist toolCalls={toolCalls} />}
                    {activeLeftTab === 'workspace' && (
                        <WorkspaceExplorer baseUrl={httpBaseUrl} sessionId={chatId} context={context} />
                    )}
                </div>
            </div>

            {/* Floating button */}
            {!isOpen && (
                <button
                    type="button"
                    {...rightFloatingButton}
                    className={`rp-floating-btn ${hasPendingAction ? 'attention' : ''} ${rightFloatingButton.className}`}
                    title="Open Agent Tool Calls (drag to move)"
                    aria-label="Open Agent Tool Calls"
                >
                    {hasPendingAction ? (
                        <i className="fa-solid fa-triangle-exclamation" style={{ animation: 'rp-blink 1.2s infinite' }}></i>
                    ) : (
                        <i className="fa-solid fa-code"></i>
                    )}
                    <NotificationBadge count={rightBadgeCount} className="badge" />
                    <span className={`ws-status ${wsStatusColor}`} title={`WebSocket: ${wsStatus}`} />
                </button>
            )}

            {/* Side Drawer Panel */}
            <div className="rp-drawer" style={{ transform: isOpen ? 'translateX(0)' : 'translateX(100%)' }}>
                <div className="rp-drawer-header">
                    <div className="rp-drawer-title-container">
                        <h4 className="rp-drawer-title">
                            <i className="fa-solid fa-robot" style={{ color: '#00b4d8' }}></i>
                            Agent Tool Calls
                        </h4>
                        <div className="rp-drawer-subtitle" title={chatId || 'No active chat'}>
                            Session: {chatId || 'None'}
                        </div>
                    </div>
                    <div className="rp-actions">
                        {toolCalls.length > 0 && (
                            <button className="rp-clear-btn" onClick={handleClearCalls}>
                                Clear
                            </button>
                        )}
                        <button className="rp-close-btn" onClick={() => setIsOpen(false)}>
                            <i className="fa-solid fa-xmark"></i>
                        </button>
                    </div>
                </div>

                <div className="rp-drawer-body" ref={panelRef}>
                    {wsStatus === 'disconnected' && (
                        <div style={{ color: '#fbbf24', fontSize: '0.78rem', textAlign: 'center', background: 'rgba(251, 191, 36, 0.08)', padding: '8px 12px', borderRadius: '6px', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '6px' }}>
                            <i className="fa-solid fa-plug-circle-xmark"></i>
                            WebSocket disconnected — real-time updates paused
                        </div>
                    )}

                    {agentStatus?.active && statusText && (
                        <div className="rp-status-strip">
                            <i className="fa-solid fa-circle-notch fa-spin"></i>
                            <span>{statusText}</span>
                        </div>
                    )}

                    {/* Pending Actions */}
                    {pendingClarifies.map((pendingClarify) => (
                        <ClarifyCard
                            key={serverRequestKey(pendingClarify)}
                            pendingClarify={pendingClarify}
                            onSubmit={handleClarifySubmit}
                            resetKey={serverRequestResetTokens[pendingClarify.rpc_id] || 0}
                        />
                    ))}

                    {pendingSudos.map((pendingSudo) => (
                        <SudoPasswordCard
                            key={serverRequestKey(pendingSudo)}
                            pendingSudo={pendingSudo}
                            onSubmit={handleSudoSubmit}
                            resetKey={serverRequestResetTokens[pendingSudo.rpc_id] || 0}
                        />
                    ))}

                    {pendingApprovals.map((approval) => (
                        <ApprovalCard
                            key={serverRequestKey(approval)}
                            approval={approval}
                            onSubmit={handleApprovalSubmit}
                            resetKey={serverRequestResetTokens[approval.rpc_id] || 0}
                        />
                    ))}

                    <div className="rp-session-card">
                        <div className="rp-session-row">
                            <span className="rp-session-label">Hermes Session</span>
                            <div className="rp-session-actions">
                                <button className="rp-icon-btn" onClick={() => { requestSessionSnapshot(); requestModelOptions(); }} title="Refresh session">
                                    <i className="fa-solid fa-rotate-right"></i>
                                </button>
                            </div>
                        </div>

                        <div className="rp-agent-controls">
                            <button
                                className="rp-agent-control-btn danger"
                                onClick={handleInterruptAgent}
                                disabled={!canControlRunningAgent || agentControlBusy.interrupt}
                                title="Interrupt the current Hermes turn"
                            >
                                <i className={`fa-solid ${agentControlBusy.interrupt ? 'fa-spinner fa-spin' : 'fa-stop'}`}></i>
                                <span>Interrupt</span>
                            </button>
                            <button
                                className="rp-agent-control-btn"
                                onClick={handleSteerAgent}
                                disabled={!canControlRunningAgent || agentControlBusy.steer}
                                title="Inject guidance after the next tool call"
                            >
                                <i className={`fa-solid ${agentControlBusy.steer ? 'fa-spinner fa-spin' : 'fa-route'}`}></i>
                                <span>Steer</span>
                            </button>
                            <button
                                className="rp-agent-control-btn"
                                onClick={handleUndoAgent}
                                disabled={!canControlIdleAgent || agentControlBusy.undo}
                                title="Undo the last user turn in SillyTavern and Hermes"
                            >
                                <i className={`fa-solid ${agentControlBusy.undo ? 'fa-spinner fa-spin' : 'fa-rotate-left'}`}></i>
                                <span>Undo</span>
                            </button>
                            <button
                                className="rp-agent-control-btn"
                                onClick={handleRetryAgent}
                                disabled={!canControlIdleAgent || agentControlBusy.retry}
                                title="Retry the last assistant reply"
                            >
                                <i className={`fa-solid ${agentControlBusy.retry ? 'fa-spinner fa-spin' : 'fa-arrows-rotate'}`}></i>
                                <span>Retry</span>
                            </button>
                            <button
                                className="rp-agent-control-btn"
                                onClick={handleCompressAgent}
                                disabled={!canControlIdleAgent || agentControlBusy.compress}
                                title="Compress Hermes session context"
                            >
                                <i className={`fa-solid ${agentControlBusy.compress ? 'fa-spinner fa-spin' : 'fa-compress'}`}></i>
                                <span>Compress</span>
                            </button>
                            <button
                                className="rp-agent-control-btn"
                                onClick={() => setIsPersonaPanelOpen(prev => !prev)}
                                title="Edit and version the active SillyTavern persona description"
                            >
                                <i className="fa-solid fa-id-card-clip"></i>
                                <span>Persona</span>
                            </button>
                        </div>

                        <PersonaPanel
                            context={context}
                            chatId={chatId}
                            isOpen={isPersonaPanelOpen}
                            onOpenChange={setIsPersonaPanelOpen}
                        />

                        {agentControlMessage && (
                            <div className={`rp-agent-control-feedback ${agentControlMessage.tone === 'error' ? 'error' : ''}`}>
                                <i className={`fa-solid ${agentControlMessage.tone === 'error' ? 'fa-triangle-exclamation' : 'fa-circle-check'}`}></i>
                                <span>{agentControlMessage.text}</span>
                            </div>
                        )}

                        <div>
                            <div className="rp-session-row" style={{ marginBottom: '6px' }}>
                                <span className="rp-session-label">Profile</span>
                                <span className="rp-session-value" title={currentProfile}>{currentProfile}</span>
                            </div>
                            <select
                                className="rp-model-select"
                                value={selectedProfile}
                                onChange={handleProfileChange}
                                disabled={wsStatus !== 'connected'}
                                title="Hermes profile for this chat; changing it rebuilds the Hermes session"
                            >
                                {profileOptions.length === 0 && <option value={selectedProfile}>{selectedProfile}</option>}
                                {profileOptions.map((profile) => (
                                    <option key={profile.name} value={profile.name} title={profile.description || profile.name}>
                                        {profile.name}
                                    </option>
                                ))}
                            </select>
                        </div>

                        <div>
                            <div className="rp-session-row" style={{ marginBottom: '6px' }}>
                                <span className="rp-session-label">Model</span>
                                <span className="rp-session-value" title={currentModel || 'Auto'}>
                                    {currentModel || 'Auto'}
                                </span>
                            </div>
                            <select
                                className="rp-model-select"
                                value={selectedModel}
                                onChange={handleModelChange}
                                disabled={wsStatus !== 'connected'}
                                title="Hermes model override"
                            >
                                <option value="">{currentModel ? `Auto (${currentModel})` : 'Auto'}</option>
                                {selectedModelOptions.map((model) => (
                                    <option key={model.id} value={model.id}>{model.label}</option>
                                ))}
                            </select>
                        </div>

                        <div>
                            <div className="rp-session-row" style={{ marginBottom: '6px' }}>
                                <span className="rp-session-label">Context</span>
                                <span className="rp-session-value">
                                    {contextMax ? `${formatTokenCount(contextUsed)} / ${formatTokenCount(contextMax)} (${contextPercent}%)` : 'Waiting for usage'}
                                </span>
                            </div>
                            <div className="rp-usage-bar" title={contextMax ? `${formatTokenCount(contextRemaining)} tokens left` : 'Usage appears after Hermes completes a turn'}>
                                <div className={`rp-usage-fill ${usageTone}`} style={{ width: `${contextPercent || 0}%` }} />
                            </div>
                            {isStreaming && (
                                <div className="rp-streaming-estimate" title={`Streaming ~${formatTokenCount(streamingEstimate.outputTokens)} output tokens`}>
                                    ~{formatTokenCount(streamingEstimate.outputTokens)} tokens streaming
                                </div>
                            )}
                        </div>

                        <div className="rp-session-grid">
                            <div className="rp-metric">
                                <span className="rp-session-label">Left</span>
                                <span className="rp-session-value">{contextMax ? formatTokenCount(contextRemaining) : '—'}</span>
                            </div>
                            <div className="rp-metric">
                                <span className="rp-session-label">Input</span>
                                <span className="rp-session-value" title="Cumulative session input tokens">{formatTokenCount(inputTokens)}</span>
                            </div>
                            <div className="rp-metric">
                                <span className="rp-session-label">Output</span>
                                <span className="rp-session-value" title={isStreaming ? "Estimated (streaming)" : "Cumulative session output tokens"}>
                                    {formatTokenCount(estimatedOutputTokens)}{isStreaming ? ' ~' : ''}
                                </span>
                            </div>
                            {Number(reasoningTokens) > 0 && (
                                <div className="rp-metric">
                                    <span className="rp-session-label">Reasoning</span>
                                    <span className="rp-session-value">{formatTokenCount(reasoningTokens)}</span>
                                </div>
                            )}
                            {Number(cacheReadTokens) > 0 && (
                                <div className="rp-metric">
                                    <span className="rp-session-label">Cache R</span>
                                    <span className="rp-session-value" title="Cache read tokens">{formatTokenCount(cacheReadTokens)}</span>
                                </div>
                            )}
                            {Number(cacheWriteTokens) > 0 && (
                                <div className="rp-metric">
                                    <span className="rp-session-label">Cache W</span>
                                    <span className="rp-session-value" title="Cache write tokens">{formatTokenCount(cacheWriteTokens)}</span>
                                </div>
                            )}
                        </div>

                        <div className="rp-session-row">
                            <span className="rp-muted">Requests: {formatTokenCount(sessionInfo.total_requests || 0)}</span>
                            {sessionInfo.reasoning_effort && (
                                <span className="rp-muted">Reasoning: {sessionInfo.reasoning_effort}</span>
                            )}
                            {turnsRemaining > 0 && contextMax > 0 && (
                                <span className={`rp-muted ${contextPercent >= 90 ? 'rp-context-critical' : contextPercent >= 75 ? 'rp-context-warn' : ''}`} title={`~${turnsRemaining} turns remaining before context overflow (avg ${formatTokenCount(avgTurnTokens)} tokens/turn)`}>
                                    ~{turnsRemaining} turns left
                                </span>
                            )}
                            {costUsd > 0 && (
                                <span className="rp-muted" title="Estimated session cost">
                                    ${Number(costUsd).toFixed(4)}
                                </span>
                            )}
                            {compressions > 0 && (
                                <span className="rp-muted" title="Context compressions">{compressions} compressions</span>
                            )}
                        </div>
                    </div>

                    <div className="rp-history-controls">
                        <select value={toolCallFilters.status} onChange={handleToolCallFilterChange('status')} title="Filter by status">
                            <option value="">All status</option>
                            <option value="running">Running</option>
                            <option value="completed">Completed</option>
                            <option value="error">Error</option>
                        </select>
                        <input
                            type="text"
                            value={toolCallFilters.tool}
                            onChange={handleToolCallFilterChange('tool')}
                            placeholder="Tool name"
                            title="Filter by tool name"
                        />
                        <input
                            type="text"
                            value={toolCallFilters.q}
                            onChange={handleToolCallFilterChange('q')}
                            placeholder="Search arguments/results"
                            title="Search arguments and results"
                            style={{ gridColumn: '1 / -1' }}
                        />
                        <div className="rp-history-actions">
                            <span>
                                {toolCallsPage.total ? `${visibleToolCalls.length} shown / ${toolCallsPage.total} matched` : `${visibleToolCalls.length} shown`}
                            </span>
                            <span style={{ display: 'flex', gap: '8px' }}>
                                <button className="rp-icon-btn" onClick={() => fetchToolCallsPage({ offset: 0, append: false })} title="Refresh history" disabled={toolCallsLoading}>
                                    <i className={`fa-solid fa-rotate-right ${toolCallsLoading ? 'fa-spin' : ''}`}></i>
                                </button>
                                <button className="rp-clear-btn" onClick={handleLoadOlderToolCalls} disabled={toolCallsLoading || !toolCallsPage.has_more}>
                                    Load older
                                </button>
                            </span>
                        </div>
                    </div>

                    {visibleToolCalls.length === 0 ? (
                        <div className="rp-empty-state">
                            <i className="fa-solid fa-terminal"></i>
                            <div style={{ fontWeight: 500, color: '#475569' }}>{hasToolCallFilters ? 'No matching tool calls' : 'No tool calls yet'}</div>
                            <div style={{ fontSize: '0.78rem', color: '#334155' }}>{hasToolCallFilters ? 'Adjust filters or refresh the history page.' : 'Tool execution by the agent will appear here in real-time.'}</div>
                        </div>
                    ) : (
                        reversedCalls.map((call, idx) => {
                            const isNewest = idx === 0;
                            return (
                                <div
                                    key={call.id || idx}
                                    className={`rp-call-card${isNewest ? ' newest' : ''}`}
                                    ref={isNewest ? latestCallRef : null}
                                >
                                    <div className="rp-card-header">
                                        <div className="rp-tool-name">
                                            <i className={toolIconClass(call.name)}></i>
                                            {call.name}
                                        </div>
                                        <span className={`rp-status-badge rp-status-${call.status}`}>
                                            {call.status === 'running' && <i className="fa-solid fa-spinner fa-spin" style={{ marginRight: '4px' }}></i>}
                                            {call.status}
                                        </span>
                                    </div>

                                    {call.arguments && (
                                        <div className="rp-code-container">
                                            <div className="rp-code-label">
                                                <i className="fa-solid fa-circle-info"></i> Arguments
                                            </div>
                                            <pre className="rp-code-block">{call.arguments}</pre>
                                        </div>
                                    )}

                                    {call.output && (
                                        <div className="rp-code-container rp-output-block">
                                            <div className="rp-code-label">
                                                <i className="fa-solid fa-reply"></i> Result
                                            </div>
                                            <pre className="rp-code-block">{call.output}</pre>
                                        </div>
                                    )}

                                    <span className="rp-timestamp">
                                        <i className="fa-regular fa-clock"></i>
                                        {formatTimestamp(call.timestamp)}
                                    </span>
                                </div>
                            );
                        })
                    )}
                </div>
            </div>
        </div>
    );
}

export default ToolCallsPanel;
