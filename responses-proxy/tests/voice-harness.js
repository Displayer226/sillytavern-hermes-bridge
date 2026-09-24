import React, { useCallback, useState } from 'react';
import { createRoot } from 'react-dom/client';
import VoiceCall, { appendVoiceResponseDelta, applyVoiceMessage } from '../src/VoiceCall';
import VoiceCallTrigger from '../src/VoiceCallTrigger';
import useDraggableFloatingButton from '../src/useDraggableFloatingButton';

const listeners = new Map();
const ctx = {
    chat: [], name1: 'User', name2: 'Hermes', characterId: 0,
    characters: [{ description: 'Tu es Hermes.', personality: 'Francophone' }],
    getCurrentChatId: () => 'mobile-chat',
    extensionSettings: { responsesProxy: { selectedModel: 'test-model', profileByChat: { 'mobile-chat': 'local' } } },
    chatCompletionSettings: { custom_url: 'https://voice-test.invalid', chat_completion_source: 'custom' },
    eventTypes: { CHAT_CHANGED: 'chat_changed', CHAT_LOADED: 'chat_loaded' },
    eventSource: {
        on: (event, handler) => { if (!listeners.has(event)) listeners.set(event, new Set()); listeners.get(event).add(handler); },
        removeListener: (event, handler) => listeners.get(event)?.delete(handler),
    },
    addOneMessage: () => {}, updateMessageBlock: () => {}, saveChat: async () => {},
};
window.SillyTavern = { getContext: () => ctx };
window.testVoiceDelta = (event) => appendVoiceResponseDelta(event, 'mobile-chat');
window.testVoiceFinal = (event, key) => applyVoiceMessage(ctx, event, key);
window.testVoiceChat = ctx.chat;
window.testSetVoiceModel = (value) => { ctx.extensionSettings.responsesProxy.selectedModel = value; };

function PeerButton({ name, className }) {
    const floatingButton = useDraggableFloatingButton(`responses-proxy-floating-${name}`, () => {});
    return <button
        type="button"
        {...floatingButton}
        className={`rp-test-peer ${className} ${floatingButton.className}`}
        data-floating-name={name}
        aria-label={`Test ${name}`}
    />;
}

function Harness() {
    const [voiceOpen, setVoiceOpen] = useState(false);
    const [voiceActive, setVoiceActive] = useState(false);
    const openVoice = useCallback(() => setVoiceOpen(true), []);

    return <>
        <style>{`
            .rp-test-peer { position:absolute; right:10px; width:44px; height:44px; pointer-events:auto; touch-action:none; transition:left 220ms cubic-bezier(0.22, 1, 0.36, 1), top 220ms cubic-bezier(0.22, 1, 0.36, 1), right 220ms cubic-bezier(0.22, 1, 0.36, 1), bottom 220ms cubic-bezier(0.22, 1, 0.36, 1), transform 160ms ease, box-shadow 160ms ease; }
            .rp-test-peer.rp-is-dragging { transition:none; }
            .rp-test-peer-middle:not(.rp-has-custom-position) { bottom:75px; }
            .rp-test-peer-bottom:not(.rp-has-custom-position) { bottom:15px; }
        `}</style>
        <VoiceCallTrigger active={voiceActive} open={voiceOpen} onActivate={openVoice} />
        <PeerButton name="middle" className="rp-test-peer-middle" />
        <PeerButton name="bottom" className="rp-test-peer-bottom" />
        <VoiceCall open={voiceOpen} onOpenChange={setVoiceOpen} onActiveChange={setVoiceActive} />
    </>;
}

createRoot(document.getElementById('root')).render(<React.StrictMode><Harness /></React.StrictMode>);
