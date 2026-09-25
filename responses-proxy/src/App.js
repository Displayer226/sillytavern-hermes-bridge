import React, { useState, useEffect } from 'react';
import wsService from './WebSocketService';

const DEFAULT_NOTIFICATION_SETTINGS = {
    clarify: true,
    approval: true,
    sudo: true,
    toolError: true,
};

const DEFAULT_PERSONA_SETTINGS = {
    maxVersions: 30,
    autoApplyAgentPatches: false,
    historyByAvatar: {},
};

function App({ context }) {
    // Read initial settings from SillyTavern context
    const initialSettings = context.extensionSettings.responsesProxy || {
        isEnabled: true,
        wsUrl: '',
        wsToken: '',
        selectedModel: '',
        themeMode: 'system',
        browserNotifications: DEFAULT_NOTIFICATION_SETTINGS,
        persona: DEFAULT_PERSONA_SETTINGS,
    };

    const [isEnabled, setIsEnabled] = useState(initialSettings.isEnabled);
    const [chatId, setChatId] = useState('No active chat');
    const [wsUrl, setWsUrl] = useState(initialSettings.wsUrl || '');
    const [wsToken, setWsToken] = useState(initialSettings.wsToken || '');
    const [selectedModel, setSelectedModel] = useState(initialSettings.selectedModel || '');
    const [themeMode, setThemeMode] = useState(initialSettings.themeMode || 'system');
    const [browserNotifications, setBrowserNotifications] = useState({
        ...DEFAULT_NOTIFICATION_SETTINGS,
        ...(initialSettings.browserNotifications || {}),
    });
    const [wsStatus, setWsStatus] = useState('disconnected');
    const [showWsSettings, setShowWsSettings] = useState(false);

    // Track WS status
    useEffect(() => {
        const unsubConnected = wsService.on('connected', () => setWsStatus('connected'));
        const unsubDisconnected = wsService.on('disconnected', () => setWsStatus('disconnected'));
        const unsubError = wsService.on('error', () => setWsStatus('disconnected'));

        return () => {
            unsubConnected();
            unsubDisconnected();
            unsubError();
        };
    }, []);

    // Update chatId when chat changes
    useEffect(() => {
        const updateChat = () => {
            const currentChatId = typeof context.getCurrentChatId === 'function' ? context.getCurrentChatId() : context.chatId;
            setChatId(currentChatId || 'No active chat');
        };

        updateChat();

        // Listen to SillyTavern chat load/change events
        context.eventSource.on(context.eventTypes.CHAT_CHANGED, updateChat);
        context.eventSource.on(context.eventTypes.CHAT_LOADED, updateChat);

        return () => {
            context.eventSource.removeListener(context.eventTypes.CHAT_CHANGED, updateChat);
            context.eventSource.removeListener(context.eventTypes.CHAT_LOADED, updateChat);
        };
    }, [context]);

    // Save settings helper
    const saveSettings = (updated) => {
        const nextSettings = {
            ...context.extensionSettings.responsesProxy,
            isEnabled: updated.isEnabled ?? isEnabled,
            wsUrl: updated.wsUrl !== undefined ? updated.wsUrl : wsUrl,
            wsToken: updated.wsToken !== undefined ? updated.wsToken : wsToken,
            selectedModel: updated.selectedModel !== undefined ? updated.selectedModel : selectedModel,
            themeMode: updated.themeMode !== undefined ? updated.themeMode : themeMode,
            browserNotifications: updated.browserNotifications !== undefined
                ? updated.browserNotifications
                : browserNotifications,
            persona: context.extensionSettings.responsesProxy?.persona || DEFAULT_PERSONA_SETTINGS,
        };
        context.extensionSettings.responsesProxy = nextSettings;
        context.saveSettingsDebounced();
        window.dispatchEvent(new CustomEvent('responses-proxy-settings-updated', { detail: nextSettings }));
    };

    const handleEnableToggle = (e) => {
        const checked = e.target.checked;
        setIsEnabled(checked);
        saveSettings({ isEnabled: checked });
    };

    const handleWsUrlChange = (e) => {
        const value = e.target.value;
        setWsUrl(value);
        saveSettings({ wsUrl: value });
    };

    const handleWsTokenChange = (e) => {
        const value = e.target.value;
        setWsToken(value);
        saveSettings({ wsToken: value });
    };

    const handleSelectedModelChange = (e) => {
        const value = e.target.value;
        setSelectedModel(value);
        saveSettings({ selectedModel: value });
    };

    const handleThemeModeChange = (e) => {
        const value = e.target.value;
        setThemeMode(value);
        saveSettings({ themeMode: value });
    };

    const handleNotificationToggle = (key) => (e) => {
        if (e.target.checked && 'Notification' in window && Notification.permission === 'default') {
            Notification.requestPermission().catch((error) => {
                console.warn('[Hermes Bridge] Notification permission request failed:', error);
            });
        }
        const next = {
            ...browserNotifications,
            [key]: e.target.checked,
        };
        setBrowserNotifications(next);
        saveSettings({ browserNotifications: next });
    };

    // Derive what the auto-detected URL would be (for display)
    const getDerivedUrl = () => {
        try {
            return wsService.constructor.deriveUrl(context);
        } catch (e) {
            return '(could not derive)';
        }
    };

    const statusColor = wsStatus === 'connected' ? '#34d399' : '#f87171';
    const statusText = wsStatus === 'connected' ? 'Connected' : 'Disconnected';

    return (
        <div className="inline-drawer wide100p flexFlowColumn" style={{ marginBottom: '15px' }}>
            <div className="inline-drawer-toggle inline-drawer-header">
                <span style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <i className="fa-solid fa-network-wired" style={{ color: '#00b4d8' }}></i>
                    <b>Hermes Bridge Session</b>
                </span>
                <div className="fa-solid fa-circle-chevron-down inline-drawer-icon down"></div>
            </div>

            <div className="inline-drawer-content" style={{ display: 'none', padding: '15px', background: 'rgba(0, 0, 0, 0.15)' }}>
                {/* Enable checkbox */}
                <div style={{ display: 'flex', alignItems: 'center', marginBottom: '15px' }}>
                    <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', userSelect: 'none' }}>
                        <input
                            type="checkbox"
                            checked={isEnabled}
                            onChange={handleEnableToggle}
                            style={{ width: '16px', height: '16px', margin: 0, cursor: 'pointer' }}
                        />
                        <span style={{ fontWeight: '500' }}>Enable Metadata Injection</span>
                    </label>
                </div>

                {isEnabled && (
                    <>
                        {/* Preview panel */}
                        <div style={{ marginTop: '8px' }}>
                            <label style={{ display: 'block', marginBottom: '12px' }}>
                                <span style={{ display: 'block', fontSize: '0.82em', opacity: 0.8, marginBottom: '6px' }}>
                                    Hermes model override
                                </span>
                                <input
                                    type="text"
                                    value={selectedModel}
                                    onChange={handleSelectedModelChange}
                                    placeholder="Auto, or e.g. anthropic/claude-sonnet-4"
                                    style={{
                                        width: '100%',
                                        background: 'rgba(0, 0, 0, 0.24)',
                                        border: '1px solid rgba(255, 255, 255, 0.1)',
                                        borderRadius: '6px',
                                        padding: '7px 9px',
                                        fontSize: '0.84em',
                                        color: '#e2e8f0',
                                        fontFamily: 'monospace',
                                        outline: 'none',
                                    }}
                                />
                            </label>
                            <div style={{ fontSize: '0.85em', opacity: 0.8, marginBottom: '6px' }}>
                                Metadata Payload Preview:
                            </div>
                            <div style={{
                                background: 'rgba(0, 0, 0, 0.3)',
                                border: '1px solid rgba(255, 255, 255, 0.08)',
                                borderRadius: '6px',
                                padding: '10px',
                                fontFamily: 'Courier New, Courier, monospace',
                                fontSize: '0.85em',
                                color: '#e0e0e0',
                                whiteSpace: 'pre-wrap',
                                wordBreak: 'break-all',
                                boxShadow: 'inset 0 1px 4px rgba(0,0,0,0.4)'
                            }}>
                                <span style={{ color: '#00b4d8' }}>st_proxy</span>:<br />
                                &nbsp;&nbsp;<span style={{ color: '#90e0ef' }}>session_id</span>: "{chatId}"
                                {selectedModel && (
                                    <>
                                        <br />
                                        &nbsp;&nbsp;<span style={{ color: '#90e0ef' }}>model</span>: "{selectedModel}"
                                    </>
                                )}
                            </div>
                        </div>

                        {/* WebSocket status */}
                        <div style={{
                            marginTop: '15px',
                            padding: '10px 12px',
                            background: 'rgba(0, 0, 0, 0.2)',
                            borderRadius: '6px',
                            border: '1px solid rgba(255, 255, 255, 0.06)',
                        }}>
                            <div style={{
                                display: 'flex',
                                alignItems: 'center',
                                justifyContent: 'space-between',
                                marginBottom: '6px',
                            }}>
                                <span style={{ fontSize: '0.82em', fontWeight: '500', color: '#94a3b8' }}>
                                    <i className="fa-solid fa-plug" style={{ marginRight: '6px' }}></i>
                                    WebSocket
                                </span>
                                <span style={{
                                    fontSize: '0.75em',
                                    color: statusColor,
                                    fontWeight: 600,
                                    display: 'flex',
                                    alignItems: 'center',
                                    gap: '5px',
                                }}>
                                    <span style={{
                                        width: '7px',
                                        height: '7px',
                                        borderRadius: '50%',
                                        background: statusColor,
                                        display: 'inline-block',
                                    }} />
                                    {statusText}
                                </span>
                            </div>

                            {/* Toggle WS settings */}
                            <button
                                onClick={() => setShowWsSettings(!showWsSettings)}
                                style={{
                                    background: 'none',
                                    border: 'none',
                                    color: '#64748b',
                                    fontSize: '0.72em',
                                    cursor: 'pointer',
                                    padding: '2px 0',
                                    display: 'flex',
                                    alignItems: 'center',
                                    gap: '4px',
                                }}
                            >
                                <i className={`fa-solid fa-chevron-${showWsSettings ? 'down' : 'right'}`} style={{ fontSize: '0.6em' }}></i>
                                Advanced: WebSocket URL override
                            </button>

                            {showWsSettings && (
                                <div style={{ marginTop: '8px' }}>
                                    <div style={{
                                        fontSize: '0.7em',
                                        color: '#64748b',
                                        marginBottom: '6px',
                                        lineHeight: '1.4',
                                    }}>
                                        Leave empty to auto-detect from your Custom OpenAI URL.
                                        <br />
                                        Auto-detected: <code style={{ color: '#94a3b8' }}>{getDerivedUrl()}</code>
                                    </div>
                                    <input
                                        type="text"
                                        value={wsUrl}
                                        onChange={handleWsUrlChange}
                                        placeholder="e.g. ws://localhost:8010/ws"
                                        style={{
                                            width: '100%',
                                            background: 'rgba(0, 0, 0, 0.3)',
                                            border: '1px solid rgba(255, 255, 255, 0.1)',
                                            borderRadius: '4px',
                                            padding: '6px 8px',
                                            fontSize: '0.8em',
                                            color: '#e2e8f0',
                                            fontFamily: 'monospace',
                                            outline: 'none',
                                        }}
                                    />
                                    <div style={{
                                        fontSize: '0.65em',
                                        color: '#475569',
                                        marginTop: '4px',
                                        lineHeight: '1.4',
                                    }}>
                                        Common values:<br />
                                        • Local: <code>ws://localhost:8010/ws</code><br />
                                        • Tailscale: <code>ws://100.x.x.x:8010/ws</code><br />
                                        • Reverse proxy: <code>wss://your-domain.com/proxy-ws/ws</code>
                                    </div>

                                    {/* Proxy Token */}
                                    <div style={{ marginTop: '10px' }}>
                                        <div style={{
                                            fontSize: '0.7em',
                                            color: '#64748b',
                                            marginBottom: '4px',
                                        }}>
                                            Proxy Token (WS_TOKEN)
                                        </div>
                                        <input
                                            type="password"
                                            value={wsToken}
                                            onChange={handleWsTokenChange}
                                            placeholder="Leave empty if no auth is configured"
                                            style={{
                                                width: '100%',
                                                background: 'rgba(0, 0, 0, 0.3)',
                                                border: '1px solid rgba(255, 255, 255, 0.1)',
                                                borderRadius: '4px',
                                                padding: '6px 8px',
                                                fontSize: '0.8em',
                                                color: '#e2e8f0',
                                                fontFamily: 'monospace',
                                                outline: 'none',
                                            }}
                                        />
                                    </div>
                                </div>
                            )}
                        </div>

                        <div style={{
                            marginTop: '15px',
                            padding: '10px 12px',
                            background: 'rgba(0, 0, 0, 0.2)',
                            borderRadius: '6px',
                            border: '1px solid rgba(255, 255, 255, 0.06)',
                        }}>
                            <label style={{ display: 'block', marginBottom: '10px' }}>
                                <span style={{ display: 'block', fontSize: '0.82em', opacity: 0.8, marginBottom: '6px' }}>
                                    Panel theme
                                </span>
                                <select
                                    value={themeMode}
                                    onChange={handleThemeModeChange}
                                    style={{
                                        width: '100%',
                                        background: 'rgba(0, 0, 0, 0.3)',
                                        border: '1px solid rgba(255, 255, 255, 0.1)',
                                        borderRadius: '4px',
                                        padding: '6px 8px',
                                        fontSize: '0.8em',
                                        color: '#e2e8f0',
                                    }}
                                >
                                    <option value="system">System</option>
                                    <option value="dark">Dark</option>
                                    <option value="light">Light</option>
                                </select>
                            </label>
                            <div style={{ fontSize: '0.82em', opacity: 0.8, marginBottom: '8px' }}>
                                Browser notifications
                            </div>
                            {[
                                ['clarify', 'Clarify requests'],
                                ['approval', 'Approval requests'],
                                ['sudo', 'Sudo password requests'],
                                ['toolError', 'Tool failures'],
                            ].map(([key, label]) => (
                                <label key={key} style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', marginTop: '7px' }}>
                                    <input
                                        type="checkbox"
                                        checked={browserNotifications[key] !== false}
                                        onChange={handleNotificationToggle(key)}
                                        style={{ width: '15px', height: '15px', margin: 0 }}
                                    />
                                    <span style={{ fontSize: '0.82em' }}>{label}</span>
                                </label>
                            ))}
                        </div>
                    </>
                )}
            </div>
        </div>
    );
}

export default App;
