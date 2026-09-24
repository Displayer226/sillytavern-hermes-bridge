import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import wsService, { deriveWsUrl } from './WebSocketService';
import ErrorBoundary from './ErrorBoundary';
import { proxyFetch } from './ProxyHttp';
/* global SillyTavern */

const context = SillyTavern.getContext();

const DEFAULT_NOTIFICATION_SETTINGS = {
    clarify: true,
    approval: true,
    toolError: true,
};

const DEFAULT_PERSONA_SETTINGS = {
    maxVersions: 30,
    autoApplyAgentPatches: false,
    historyByAvatar: {},
};

// Track registered event handlers for cleanup on extension unload
const _registeredHandlers = [];
const pendingHermesSwipeSyncByChat = new Map();
const pendingGenerationTypeByChat = new Map();

function registerEventHandler(eventSource, eventType, handler) {
    eventSource.on(eventType, handler);
    _registeredHandlers.push({ eventSource, eventType, handler });
}

function cleanupEventHandlers() {
    for (const { eventSource, eventType, handler } of _registeredHandlers) {
        try {
            eventSource.removeListener(eventType, handler);
        } catch (e) {
            if (e instanceof TypeError || e instanceof ReferenceError || e instanceof SyntaxError) {
                throw e; // Don't ignore critical errors
            }
            console.warn('[Responses Proxy] Failed to remove event handler:', e);
        }
    }
    _registeredHandlers.length = 0;
}

function isPlainObject(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function injectSessionMetadata(parsed, chatId) {
    const settings = context.extensionSettings.responsesProxy || {};
    const selectedModel = typeof settings.selectedModel === 'string' ? settings.selectedModel.trim() : '';
    const userName = typeof context.name1 === 'string' ? context.name1.trim() : '';
    const characterName = typeof context.name2 === 'string' ? context.name2.trim() : '';
    const selectedProfile = typeof settings.profileByChat?.[chatId] === 'string'
        ? settings.profileByChat[chatId].trim().toLowerCase()
        : '';
    // Workspace paths are relative to the proxy's explicitly configured workspace root.
    // Never let the browser submit an arbitrary host path as a Hermes CWD.
    const selectedWorkspace = typeof settings.workspaceByChat?.[chatId] === 'string'
        ? settings.workspaceByChat[chatId].trim().replace(/^\/+|\/+$/g, '')
        : '';
    const applySelectedWorkspace = (metadata) => {
        if (selectedWorkspace) {
            metadata.workspace = selectedWorkspace;
        } else {
            delete metadata.workspace;
        }
        return metadata;
    };
    const applyParticipantNames = (metadata) => {
        if (userName) metadata.user_name = userName;
        else delete metadata.user_name;
        if (characterName) metadata.character_name = characterName;
        else delete metadata.character_name;
        return metadata;
    };

    if (Array.isArray(parsed)) {
        const existingItem = parsed.find((item) => isPlainObject(item) && isPlainObject(item.st_proxy));
        if (existingItem) {
            existingItem.st_proxy = {
                ...existingItem.st_proxy,
                session_id: chatId,
            };
            if (selectedModel) {
                existingItem.st_proxy.model = selectedModel;
            } else {
                delete existingItem.st_proxy.model;
            }
            if (selectedProfile) existingItem.st_proxy.profile = selectedProfile;
            else delete existingItem.st_proxy.profile;
            applySelectedWorkspace(existingItem.st_proxy);
            applyParticipantNames(existingItem.st_proxy);
            return { body: parsed, metadata: existingItem.st_proxy };
        }

        const metadata = { session_id: chatId };
        if (selectedModel) {
            metadata.model = selectedModel;
        }
        if (selectedProfile) metadata.profile = selectedProfile;
        applySelectedWorkspace(metadata);
        applyParticipantNames(metadata);
        parsed.push({ st_proxy: metadata });
        return { body: parsed, metadata };
    }

    const body = isPlainObject(parsed) ? parsed : {};
    const metadata = {
        ...(isPlainObject(body.st_proxy) ? body.st_proxy : {}),
        session_id: chatId,
    };
    if (selectedModel) {
        metadata.model = selectedModel;
    } else {
        delete metadata.model;
    }
    if (selectedProfile) metadata.profile = selectedProfile;
    else delete metadata.profile;
    applySelectedWorkspace(metadata);
    applyParticipantNames(metadata);
    body.st_proxy = metadata;
    return { body, metadata };
}

function normalizeProxyBaseUrl(baseUrl) {
    return baseUrl.trim().replace(/\/+$/, '').replace(/\/v1$/i, '');
}

function getCurrentChatId() {
    return typeof context.getCurrentChatId === 'function' ? context.getCurrentChatId() : context.chatId;
}

function isImageContentPart(part) {
    if (!isPlainObject(part)) {
        return false;
    }
    const type = String(part.type || '').toLowerCase();
    const imageUrl = isPlainObject(part.image_url) ? part.image_url.url : part.image_url;
    return type === 'image_url' || type === 'input_image' || typeof imageUrl === 'string';
}

function messageHasImagePart(message) {
    if (!isPlainObject(message)) {
        return false;
    }
    if (typeof message.content === 'string') {
        return /data:image\/[a-z0-9.+-]+;base64,/i.test(message.content);
    }
    return Array.isArray(message.content) && message.content.some(isImageContentPart);
}

function getLatestUserMessage(messages) {
    if (!Array.isArray(messages)) {
        return null;
    }
    for (let index = messages.length - 1; index >= 0; index--) {
        const message = messages[index];
        if (isPlainObject(message) && String(message.role || '').toLowerCase() === 'user') {
            return message;
        }
    }
    return null;
}

function messageText(message) {
    if (!isPlainObject(message)) {
        return '';
    }
    if (typeof message.content === 'string') {
        return message.content;
    }
    if (!Array.isArray(message.content)) {
        return '';
    }
    return message.content
        .map((part) => {
            if (typeof part === 'string') {
                return part;
            }
            if (!isPlainObject(part)) {
                return '';
            }
            return typeof part.text === 'string' ? part.text : '';
        })
        .filter(Boolean)
        .join('\n');
}

function normalizeComparableText(text) {
    return String(text || '').replace(/\s+/g, ' ').trim();
}

function getChatMessageText(message) {
    return normalizeComparableText(message?.mes || message?.message || '');
}

function getMatchingUserMessage(messages, chatMessage) {
    const chatText = getChatMessageText(chatMessage);
    if (!chatText || !Array.isArray(messages)) {
        return null;
    }

    for (let index = messages.length - 1; index >= 0; index--) {
        const message = messages[index];
        if (!isPlainObject(message) || String(message.role || '').toLowerCase() !== 'user') {
            continue;
        }
        const candidateText = normalizeComparableText(messageText(message));
        if (candidateText && (candidateText.includes(chatText) || chatText.includes(candidateText))) {
            return message;
        }
    }
    return null;
}

function getLatestVisibleUserMessageWithMedia() {
    const chat = context.chat;
    if (!Array.isArray(chat)) {
        return null;
    }
    for (let index = chat.length - 1; index >= 0; index--) {
        const message = chat[index];
        const media = message?.extra?.media;
        if (message?.is_user && Array.isArray(media) && media.length > 0) {
            return message;
        }
    }
    return null;
}

function isImageMedia(media) {
    if (!media?.url) {
        return false;
    }
    const mediaType = String(media.type || 'image').toLowerCase();
    return mediaType === 'image' || mediaType.startsWith('image/');
}

function getImageMediaForMessage(message) {
    const media = message?.extra?.media;
    if (!Array.isArray(media) || media.length === 0) {
        return [];
    }

    const display = String(message?.extra?.media_display || context.powerUserSettings?.media_display || 'list').toLowerCase();
    if (display === 'gallery') {
        const mediaIndex = Number(message?.extra?.media_index);
        const selected = Number.isInteger(mediaIndex) && mediaIndex >= 0 && mediaIndex < media.length
            ? media[mediaIndex]
            : media[media.length - 1];
        return isImageMedia(selected) ? [selected] : [];
    }

    return media.filter(isImageMedia);
}

function ensureMessageContentArray(message) {
    if (Array.isArray(message.content)) {
        return message.content;
    }

    const text = message.content;
    message.content = [];
    if (typeof text === 'string' && text.trim()) {
        message.content.push({ type: 'text', text });
    }
    return message.content;
}

function imageMimeTypeFromName(name) {
    const normalized = String(name || '').split(/[?#]/, 1)[0].toLowerCase();
    if (/\.(jpe?g|jfif)$/.test(normalized)) return 'image/jpeg';
    if (/\.png$/.test(normalized)) return 'image/png';
    if (/\.gif$/.test(normalized)) return 'image/gif';
    if (/\.webp$/.test(normalized)) return 'image/webp';
    if (/\.bmp$/.test(normalized)) return 'image/bmp';
    if (/\.svg$/.test(normalized)) return 'image/svg+xml';
    return '';
}

function blobToDataUrl(blob) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ''));
        reader.onerror = () => reject(reader.error || new Error('Failed to read image blob'));
        reader.readAsDataURL(blob);
    });
}

function rewriteDataUrlMime(dataUrl, mimeType) {
    const base64Marker = ';base64,';
    const markerIndex = dataUrl.indexOf(base64Marker);
    if (!mimeType || markerIndex < 0) {
        return dataUrl;
    }
    return `data:${mimeType}${dataUrl.slice(markerIndex)}`;
}

async function mediaToDataImageUrl(media) {
    const url = typeof media?.url === 'string' ? media.url.trim() : '';
    if (!url) {
        return null;
    }
    if (/^data:image\//i.test(url)) {
        return url;
    }

    const response = await fetch(url, { method: 'GET', cache: 'force-cache' });
    if (!response.ok) {
        throw new Error(`Failed to fetch image attachment: HTTP ${response.status}`);
    }

    const blob = await response.blob();
    const dataUrl = await blobToDataUrl(blob);
    if (/^data:image\//i.test(dataUrl)) {
        return dataUrl;
    }

    const mimeType = blob.type?.startsWith('image/')
        ? blob.type
        : imageMimeTypeFromName(media.title || url);
    const rewritten = rewriteDataUrlMime(dataUrl, mimeType);
    return /^data:image\//i.test(rewritten) ? rewritten : null;
}

async function appendVisibleChatImages(generateData) {
    if (!isPlainObject(generateData) || !Array.isArray(generateData.messages)) {
        return 0;
    }

    if (context.chatCompletionSettings?.media_inlining === false) {
        return 0;
    }

    const chatMessage = getLatestVisibleUserMessageWithMedia();
    const imageMedia = getImageMediaForMessage(chatMessage);
    if (imageMedia.length === 0) {
        return 0;
    }

    const targetMessage = getMatchingUserMessage(generateData.messages, chatMessage)
        || (!getChatMessageText(chatMessage) ? getLatestUserMessage(generateData.messages) : null);
    if (!targetMessage || messageHasImagePart(targetMessage)) {
        return 0;
    }

    const content = ensureMessageContentArray(targetMessage);
    const detail = context.chatCompletionSettings?.inline_image_quality || 'auto';
    let appended = 0;
    for (const media of imageMedia) {
        try {
            const imageUrl = await mediaToDataImageUrl(media);
            if (!imageUrl) {
                continue;
            }
            content.push({ type: 'image_url', image_url: { url: imageUrl, detail } });
            appended++;
        } catch (error) {
            console.warn('[Responses Proxy] Failed to inline visible chat image:', error);
        }
    }
    return appended;
}

function getLastAssistantMessageIndex() {
    const chat = context.chat;
    if (!Array.isArray(chat)) {
        return -1;
    }
    for (let index = chat.length - 1; index >= 0; index--) {
        const message = chat[index];
        if (message && !message.is_user && !message.is_system) {
            return index;
        }
    }
    return -1;
}

function markHermesSwipeSyncNeeded(messageId) {
    const chatId = getCurrentChatId();
    const chat = context.chat;
    const mesId = Number(messageId);
    if (!chatId || !Array.isArray(chat) || !Number.isInteger(mesId)) {
        return;
    }

    const message = chat[mesId];
    if (!message || message.is_user || message.is_system) {
        return;
    }

    if (mesId !== getLastAssistantMessageIndex()) {
        const text = 'Hermes sync only supports swiping the latest assistant reply.';
        console.warn('[Responses Proxy]', text, { messageId: mesId });
        SillyTavern.toastr?.warning?.(text);
        return;
    }

    const swipeId = Number(message.swipe_id ?? 0);
    const swipes = Array.isArray(message.swipes) ? message.swipes : [];
    if (!Number.isInteger(swipeId) || swipeId < 0 || swipeId >= swipes.length) {
        return;
    }

    pendingHermesSwipeSyncByChat.set(chatId, {
        messageId: mesId,
        swipeId,
        swipeCount: swipes.length,
    });
    console.log('[Responses Proxy] Marked Hermes sync for selected SillyTavern swipe:', {
        chatId,
        messageId: mesId,
        swipeId,
        swipeCount: swipes.length,
    });
}

function consumeHermesUndoBeforeSubmitReason(generateData, chatId) {
    let generationType = String(generateData?.type || '').trim().toLowerCase();
    if (!generationType) {
        generationType = pendingGenerationTypeByChat.get(chatId) || '';
    }
    pendingGenerationTypeByChat.delete(chatId);

    if (generationType === 'regenerate' || generationType === 'swipe') {
        pendingHermesSwipeSyncByChat.delete(chatId);
        return generationType;
    }

    const pending = pendingHermesSwipeSyncByChat.get(chatId);
    if (!pending) {
        return null;
    }

    pendingHermesSwipeSyncByChat.delete(chatId);
    const nextType = generationType || 'normal';
    return `selected_swipe:${pending.messageId}:${pending.swipeId}:${nextType}`;
}

// Initialize default settings if not present
if (!context.extensionSettings.responsesProxy) {
    context.extensionSettings.responsesProxy = {
        isEnabled: true,
        selectedModel: '',
        profileByChat: {},
        workspaceByChat: {},
        themeMode: 'system',
        browserNotifications: DEFAULT_NOTIFICATION_SETTINGS,
        persona: DEFAULT_PERSONA_SETTINGS,
    };
} else {
    context.extensionSettings.responsesProxy = {
        themeMode: 'system',
        browserNotifications: DEFAULT_NOTIFICATION_SETTINGS,
        persona: DEFAULT_PERSONA_SETTINGS,
        ...context.extensionSettings.responsesProxy,
        browserNotifications: {
            ...DEFAULT_NOTIFICATION_SETTINGS,
            ...(context.extensionSettings.responsesProxy.browserNotifications || {}),
        },
        workspaceByChat: isPlainObject(context.extensionSettings.responsesProxy.workspaceByChat)
            ? { ...context.extensionSettings.responsesProxy.workspaceByChat }
            : {},
        profileByChat: isPlainObject(context.extensionSettings.responsesProxy.profileByChat)
            ? { ...context.extensionSettings.responsesProxy.profileByChat }
            : {},
        persona: {
            ...DEFAULT_PERSONA_SETTINGS,
            ...(context.extensionSettings.responsesProxy.persona || {}),
            historyByAvatar: {
                ...(context.extensionSettings.responsesProxy.persona?.historyByAvatar || {}),
            },
        },
    };
}

// Force WebSocket connection early so that background tasks (like session cleanup)
// are reliably processed even before the panel is opened.
wsService.setContext(context);
const initialWsUrl = deriveWsUrl(context);
const initialChatId = getCurrentChatId();
wsService.connect(initialChatId || null, initialWsUrl);

registerEventHandler(context.eventSource, context.eventTypes.MESSAGE_SWIPED, (messageId) => {
    markHermesSwipeSyncNeeded(messageId);
});

registerEventHandler(context.eventSource, context.eventTypes.GENERATION_STARTED, (generationType, _params, isDryRun) => {
    if (isDryRun) {
        return;
    }
    const chatId = getCurrentChatId();
    if (!chatId) {
        return;
    }
    const normalizedType = String(generationType || 'normal').trim().toLowerCase() || 'normal';
    pendingGenerationTypeByChat.set(chatId, normalizedType);
});

[context.eventTypes.CHAT_CHANGED, context.eventTypes.CHAT_LOADED].filter(Boolean).forEach((eventType) => {
    registerEventHandler(context.eventSource, eventType, () => {
        pendingGenerationTypeByChat.clear();
        pendingHermesSwipeSyncByChat.clear();
    });
});

// Hook into SillyTavern's chat completion settings generation event
registerEventHandler(context.eventSource, context.eventTypes.CHAT_COMPLETION_SETTINGS_READY, async (generate_data) => {
    const settings = context.extensionSettings.responsesProxy;
    if (!settings || settings.isEnabled === false) {
        return;
    }

    // Get current chatId
    const chatId = getCurrentChatId();
    if (!chatId) {
        console.warn('[Responses Proxy] No active chat ID found. Skipping metadata injection.');
        return;
    }
    const hermesUndoReason = consumeHermesUndoBeforeSubmitReason(generate_data, chatId);

    try {
        const appendedImages = await appendVisibleChatImages(generate_data);
        if (appendedImages > 0) {
            console.log('[Responses Proxy] Added visible chat images to request:', appendedImages);
        }
    } catch (e) {
        console.warn('[Responses Proxy] Failed to add visible chat images to request:', e);
    }

    // Parse existing custom_include_body
    const yaml = SillyTavern.libs.yaml;
    let customIncludeBody = generate_data.custom_include_body || '';
    let parsed = {};

    if (customIncludeBody.trim()) {
        try {
            parsed = yaml.parse(customIncludeBody) || {};
        } catch (e) {
            console.error('[Responses Proxy] Failed to parse custom_include_body as YAML', e);
        }
    }

    // Inject/update proxy metadata while preserving st_proxy.backend and other user metadata.
    const injected = injectSessionMetadata(parsed, chatId);
    parsed = injected.body;
    if (hermesUndoReason) {
        injected.metadata.hermes_undo_before_submit = hermesUndoReason;
    } else {
        delete injected.metadata.hermes_undo_before_submit;
    }

    // Serialize back to YAML
    generate_data.custom_include_body = yaml.stringify(parsed);
    console.log('[Responses Proxy] Injected metadata structure:', injected.metadata);
});

// When a chat is deleted in SillyTavern, notify the proxy to clean up session data.
function deleteProxySession(deletedChatId) {
    if (!deletedChatId) return;
    const sessionId = String(deletedChatId).replace(/\.jsonl$/i, '').trim();
    if (!sessionId) return;

    console.log('[Responses Proxy] Chat deleted, cleaning up session:', sessionId);
    // Send cleanup command via WS if connected, otherwise fire-and-forget HTTP
    if (wsService.isConnected) {
        wsService.send({ type: 'delete_session', session_id: sessionId });
    } else {
        // Best-effort HTTP cleanup
        try {
            const settings = context.chatCompletionSettings || {};
            let baseUrl = settings.chat_completion_source === 'custom'
                ? settings.custom_url
                : settings.reverse_proxy || settings.custom_url;
            if (baseUrl) {
                baseUrl = normalizeProxyBaseUrl(baseUrl);
                const ctrl = new AbortController();
                const timer = setTimeout(() => ctrl.abort(), 5000);
                proxyFetch(context, `${baseUrl}/v1/session/${encodeURIComponent(sessionId)}`, {
                    method: 'DELETE',
                    signal: ctrl.signal,
                })
                    .finally(() => clearTimeout(timer))
                    .catch((err) => {
                        console.warn('[Responses Proxy] Failed to delete session via HTTP:', err);
                    });
            }
        } catch (e) {
            // Ignore
        }
    }
}

[
    context.eventTypes.CHAT_DELETED,
    context.eventTypes.GROUP_CHAT_DELETED,
].filter(Boolean).forEach(eventType => {
    registerEventHandler(context.eventSource, eventType, deleteProxySession);
});

// Mount the settings UI panel
const rootContainer = document.getElementById('extensions_settings');
const rootElement = document.createElement('div');
rootContainer.appendChild(rootElement);

const root = ReactDOM.createRoot(rootElement);
root.render(
    <React.StrictMode>
        <ErrorBoundary>
            <App context={context} />
        </ErrorBoundary>
    </React.StrictMode>
);

// Mount the Tool Calls floating panel
const panelContainer = document.createElement('div');
panelContainer.id = 'responses-proxy-tool-calls-root';
// Fixed positioning + full viewport ensures absolutely positioned buttons
// stay visible even when SillyTavern's body has position: fixed + overflow: hidden
Object.assign(panelContainer.style, {
    position: 'fixed',
    top: '0',
    left: '0',
    width: '100vw',
    height: '100vh',
    zIndex: '30000',
    pointerEvents: 'none',
});
document.body.appendChild(panelContainer);

const panelRoot = ReactDOM.createRoot(panelContainer);

function mountToolCallsPanel() {
    import(/* webpackChunkName: "tool-calls-panel" */ './ToolCallsPanelRoot')
        .then(({ default: ToolCallsPanelRoot }) => {
            panelRoot.render(<ToolCallsPanelRoot context={context} />);
        })
        .catch((error) => {
            console.error('[Responses Proxy] Failed to load tool calls panel:', error);
        });
}

if (typeof queueMicrotask === 'function') {
    queueMicrotask(mountToolCallsPanel);
} else {
    setTimeout(mountToolCallsPanel, 0);
}
