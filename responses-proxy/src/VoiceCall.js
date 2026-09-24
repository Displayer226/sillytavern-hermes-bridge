import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import wsService, { deriveWsUrl } from './WebSocketService';

/* global SillyTavern */
const liveContext = () => SillyTavern.getContext();
const chatId = (ctx) => ctx.getCurrentChatId?.() || ctx.chatId;
const JOURNAL_KEY = 'responses-proxy-voice-pending';

function pendingMessages() {
    try { return JSON.parse(sessionStorage.getItem(JOURNAL_KEY) || '[]'); }
    catch { return []; }
}

function renderMessage(ctx, messageId, message) {
    if (typeof ctx.updateMessageBlock === 'function') {
        ctx.updateMessageBlock(messageId, message);
    }
}

export function appendVoiceResponseDelta(event, targetChat) {
    const ctx = liveContext();
    if (String(chatId(ctx)) !== targetChat || !event.text || event.call_id == null || event.turn_id == null) return;
    let messageId = ctx.chat.findIndex((message) => message.extra?.voice_stream_turn_id === event.turn_id);
    if (messageId < 0) {
        const message = {
            name: ctx.name2,
            is_user: false,
            is_system: false,
            send_date: new Date().toISOString(),
            mes: event.text,
            extra: {
                voice_call_id: event.call_id,
                voice_stream_turn_id: event.turn_id,
                voice_stream_pending: true,
            },
        };
        ctx.chat.push(message);
        ctx.addOneMessage(message);
        return;
    }
    const message = ctx.chat[messageId];
    message.mes += event.text;
    renderMessage(ctx, messageId, message);
}

function commonPrefixLength(left, right) {
    const limit = Math.min(left.length, right.length);
    let index = 0;
    while (index < limit && left[index] === right[index]) index++;
    return index;
}

async function deleteDraft(ctx, index) {
    const beforeLength = ctx.chat.length;
    if (typeof ctx.deleteMessage === 'function') {
        await ctx.deleteMessage(index, undefined, false);
    }
    if (ctx.chat.length === beforeLength) ctx.chat.splice(index, 1);
}

async function discardStaleVoiceDrafts(ctx) {
    const draftIds = ctx.chat.map((message, index) => message.extra?.voice_stream_pending ? index : -1)
        .filter((index) => index >= 0).sort((a, b) => b - a);
    for (const index of draftIds) await deleteDraft(ctx, index);
    if (draftIds.length && typeof ctx.saveChat === 'function') await ctx.saveChat();
}

export async function applyVoiceMessage(ctx, event, key) {
    const existing = ctx.chat.some((message) => message.extra?.voice_id === key);
    const draftIds = event.role === 'assistant'
        ? ctx.chat.map((message, index) => message.extra?.voice_stream_pending
            && message.extra?.voice_call_id === event.call_id ? index : -1).filter((index) => index >= 0)
        : [];
    if (existing) {
        for (const index of draftIds.reverse()) await deleteDraft(ctx, index);
        return;
    }
    const finalText = event.text || '';
    // Several short STT commits can start and cancel generations before their
    // transcript events are saved. Prefer the draft sharing the longest prefix
    // with the spoken final text; on ties, the most recent draft wins.
    const draftId = draftIds.reduce((best, index) => {
        if (best < 0) return index;
        const score = commonPrefixLength(String(ctx.chat[index].mes || ''), finalText);
        const bestScore = commonPrefixLength(String(ctx.chat[best].mes || ''), finalText);
        return score >= bestScore ? index : best;
    }, -1);
    if (draftId >= 0) {
        const message = ctx.chat[draftId];
        message.mes = event.text || message.mes || '…';
        delete message.extra.voice_stream_pending;
        delete message.extra.voice_stream_turn_id;
        Object.assign(message.extra, {
            voice_id: key,
            voice_call_id: event.call_id,
            voice_interrupted: event.interrupted,
        });
        // Any other pending assistant bubble belongs to an abandoned generation
        // from the same call. The worker serializes transcript events before it
        // requests the next completion, so no newer legitimate draft exists yet.
        for (const index of draftIds.filter((index) => index !== draftId).sort((a, b) => b - a)) {
            await deleteDraft(ctx, index);
        }
        renderMessage(ctx, ctx.chat.indexOf(message), message);
        return;
    }
    const message = {
        name: event.role === 'user' ? ctx.name1 : ctx.name2,
        is_user: event.role === 'user', is_system: false,
        send_date: new Date().toISOString(), mes: event.text || '…',
        extra: { voice_id: key, voice_call_id: event.call_id, voice_interrupted: event.interrupted },
    };
    ctx.chat.push(message);
    ctx.addOneMessage(message);
}

async function saveVoiceEvent(event, targetChat) {
    const key = `${event.call_id}:${event.id}`;
    const pending = pendingMessages();
    if (!pending.some((entry) => entry.key === key)) {
        pending.push({ key, targetChat, event });
        sessionStorage.setItem(JOURNAL_KEY, JSON.stringify(pending));
    }
    await replayPending();
}

async function replayPending() {
    const ctx = liveContext();
    const pending = pendingMessages();
    const saved = new Set();
    for (const entry of pending) {
        if (String(chatId(ctx)) !== entry.targetChat) continue;
        await applyVoiceMessage(ctx, entry.event, entry.key);
        // Re-check before saving: a previous await may have switched the chat.
        if (String(chatId(liveContext())) !== entry.targetChat) break;
        await ctx.saveChat();
        saved.add(entry.key);
    }
    sessionStorage.setItem(JOURNAL_KEY, JSON.stringify(pendingMessages().filter((entry) => !saved.has(entry.key))));
}

function connection(ctx) {
    const url = new URL(deriveWsUrl(ctx));
    url.protocol = url.protocol === 'wss:' ? 'https:' : 'http:';
    url.pathname = url.pathname.replace(/\/ws\/?$/, '');
    url.search = '';
    const token = ctx.extensionSettings.responsesProxy?.wsToken?.trim();
    return { base: url.toString().replace(/\/$/, ''), headers: {
        'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}),
    } };
}

async function api(conn, path, options = {}) {
    const response = await fetch(`${conn.base}/v1/voice${path}`, { signal: AbortSignal.timeout(20000), ...options, headers: conn.headers });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || data.error?.message || `HTTP error ${response.status}`);
    return data;
}

function snapshot(ctx) {
    const settings = ctx.extensionSettings.responsesProxy || {};
    const id = chatId(ctx);
    const character = ctx.characters?.[ctx.characterId];
    const substitute = (text) => ctx.substituteParams?.(text) || text;
    const persona = [character?.description, character?.personality, character?.scenario,
        ctx.powerUserSettings?.persona_description].filter(Boolean).map(substitute).join('\n\n');
    const selectedModel = typeof settings.selectedModel === 'string' ? settings.selectedModel.trim() : '';
    // Same contract as text requests (index.js): with no model override the
    // field is omitted and the call inherits the backend's current model.
    return {
        session_id: String(id),
        ...(selectedModel ? { model: selectedModel } : {}),
        profile: settings.profileByChat?.[id] || '', workspace: settings.workspaceByChat?.[id] || '',
        messages: [
            ...(persona ? [{ role: 'system', content: persona }] : []),
            ...ctx.chat.filter((m) => !m.is_system).map((m) => ({
                role: m.is_user ? 'user' : 'assistant',
                content: String(m.mes || '') + (m.extra?.voice_interrupted ? '\n[Voice response interrupted here.]' : ''),
            })),
        ],
    };
}

export default function VoiceCall({ open, onOpenChange, onActiveChange }) {
    const [status, setStatus] = useState('idle');
    const [error, setError] = useState('');
    const [muted, setMuted] = useState(false);
    const [audioBlocked, setAudioBlocked] = useState(false);
    const [transcript, setTranscript] = useState('');
    const [seconds, setSeconds] = useState(0);
    const current = useRef(null);
    const serial = useRef(Promise.resolve());
    const busy = useRef(false);
    const startAttempt = useRef(0);
    const panel = useRef(null);
    const audioHost = useRef(null);
    const labels = { idle: 'Voice call', connecting: 'Connecting…', listening: 'Listening',
        thinking: 'Hermes is thinking…', speaking: 'Hermes is speaking', reconnecting: 'Reconnecting…', ending: 'Ending call…' };
    function enqueue(event, call) {
        serial.current = serial.current.catch(() => {}).then(async () => {
            if (event.call_id !== call.id) return;
            if (!event.text?.trim() && !event.interrupted) return;
            await saveVoiceEvent(event, call.chat);
        });
        serial.current.catch((err) => setError(`Transcript: ${err.message}`));
        return serial.current;
    }

    async function sync(call) {
        const data = await api(call.conn, `/calls/${call.id}/events?after=${call.sequence}`);
        if (data.error) setError(data.error);
        for (const event of data.events) {
            await enqueue(event, call);
            call.sequence = event.sequence;
        }
        return data;
    }

    async function hangup() {
        const call = current.current;
        if (!call) {
            startAttempt.current++;
            busy.current = false;
            setStatus('idle');
            return;
        }
        if (call.ending) return;
        call.ending = true;
        setStatus('ending');
        clearInterval(call.timer);
        try {
            // Disconnect first so LiveKit commits the last heard speech.
            await call.room.disconnect();
            await api(call.conn, `/calls/${call.id}`, { method: 'DELETE' });
            for (let attempt = 0; attempt < 12; attempt++) {
                const result = await sync(call);
                if (result.finished) break;
                await new Promise((resolve) => setTimeout(resolve, 500));
            }
        } catch (err) { setError(err.message); }
        finally {
            call.wakeLock?.release().catch(() => {});
            audioHost.current?.replaceChildren();
            if (current.current === call) current.current = null;
            setStatus('idle'); setMuted(false); setAudioBlocked(false);
            busy.current = false;
        }
    }

    async function start() {
        if (busy.current) return;
        busy.current = true;
        const attempt = ++startAttempt.current;
        const checkAttempt = () => { if (attempt !== startAttempt.current) throw new Error('Call cancelled.'); };
        setError(''); setTranscript(''); setStatus('connecting');
        let room;
        let reserved;
        let conn;
        try {
            const { Room, RoomEvent, Track, ParticipantKind } = await import(/* webpackChunkName: "voice-livekit" */ 'livekit-client');
            checkAttempt();
            const ctx = liveContext();
            if (!chatId(ctx) || ctx.groupId) throw new Error('Open an individual chat with your agent.');
            if (ctx.extensionSettings.responsesProxy?.isEnabled === false) throw new Error('Enable Responses Proxy in the extensions panel.');
            if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) throw new Error('Microphone access requires an HTTPS SillyTavern connection.');
            if (ctx.streamingProcessor && !ctx.streamingProcessor.isFinished) throw new Error('Wait for the current response to finish.');
            const initialChat = String(chatId(ctx));
            // Request microphone permission while still in the button gesture.
            const permission = await navigator.mediaDevices.getUserMedia({ audio: true });
            permission.getTracks().forEach((track) => track.stop());
            checkAttempt();
            conn = connection(ctx);
            const data = await api(conn, '/calls', { method: 'POST', body: JSON.stringify(snapshot(ctx)) });
            reserved = data.callId;
            checkAttempt();
            if (String(chatId(liveContext())) !== initialChat) throw new Error('The chat changed while connecting. Start the call again.');
            room = new Room({ adaptiveStream: true, dynacast: true,
                audioCaptureDefaults: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
            const call = { id: data.callId, chat: String(chatId(ctx)), conn, room, sequence: 0,
                start: Date.now(), ending: false, timer: null, wakeLock: null };
            current.current = call;
            room.on(RoomEvent.TrackSubscribed, (track) => {
                if (track.kind === Track.Kind.Audio) {
                    const audio = track.attach(); audio.autoplay = true;
                    audioHost.current?.appendChild(audio);
                }
            });
            room.on(RoomEvent.TrackUnsubscribed, (track) => track.detach().forEach((el) => el.remove()));
            room.on(RoomEvent.AudioPlaybackStatusChanged, () => setAudioBlocked(!room.canPlaybackAudio));
            room.on(RoomEvent.ParticipantAttributesChanged, (attrs, participant) => {
                if (participant.kind === ParticipantKind.AGENT && attrs['lk.agent.state']) setStatus(attrs['lk.agent.state']);
            });
            room.on(RoomEvent.Reconnecting, () => setStatus('reconnecting'));
            room.on(RoomEvent.Reconnected, () => setStatus('listening'));
            room.on(RoomEvent.Disconnected, () => { if (!call.ending) void hangup(); });
            room.registerTextStreamHandler('lk.transcription', async (reader) => {
                let text = '';
                for await (const chunk of reader) { text += chunk; setTranscript(text); }
            });
            await room.connect(data.serverUrl, data.participantToken);
            if (call.ending || String(chatId(liveContext())) !== call.chat) { await hangup(); return; }
            await room.startAudio().catch(() => setAudioBlocked(true));
            await room.localParticipant.setMicrophoneEnabled(true);
            setStatus('listening'); setSeconds(0);
            navigator.wakeLock?.request('screen').then((lock) => {
                if (call.ending) lock.release(); else call.wakeLock = lock;
            }).catch(() => {});
            call.timer = setInterval(() => {
                setSeconds(Math.floor((Date.now() - call.start) / 1000));
                if (call.syncing) return;
                call.syncing = true;
                sync(call).then((result) => { if (result.closed) void hangup(); })
                    .catch((err) => setError(err.message)).finally(() => { call.syncing = false; });
            }, 1500);
        } catch (err) {
            if (attempt !== startAttempt.current) {
                if (reserved) await api(conn, `/calls/${reserved}`, { method: 'DELETE' }).catch(() => {});
                return;
            }
            setError(err.name === 'NotAllowedError' ? 'Allow microphone access to start the call.' : err.message);
            if (current.current) await hangup();
            else {
                await room?.disconnect();
                if (reserved) await api(conn, `/calls/${reserved}`, { method: 'DELETE' }).catch(() => {});
                setStatus('idle'); busy.current = false;
            }
        }
    }

    useEffect(() => {
        const ctx = liveContext();
        const onChat = () => {
            if (current.current && String(chatId(liveContext())) !== current.current.chat) void hangup();
            serial.current = serial.current.catch(() => {}).then(replayPending);
            serial.current.catch((err) => setError(err.message));
        };
        const onVoice = (event) => { const call = current.current; if (call) void enqueue(event, call); };
        const onVoiceDelta = (event) => {
            const call = current.current;
            if (call && event.call_id === call.id) appendVoiceResponseDelta(event, call.chat);
        };
        const onPageHide = () => {
            const call = current.current;
            if (call) fetch(`${call.conn.base}/v1/voice/calls/${call.id}`, {
                method: 'DELETE', headers: call.conn.headers, keepalive: true,
            }).catch(() => {});
        };
        const events = [...new Set([ctx.eventTypes.CHAT_CHANGED, ctx.eventTypes.CHAT_LOADED].filter(Boolean))];
        events.forEach((event) => ctx.eventSource.on(event, onChat));
        wsService.on('voice_message', onVoice);
        wsService.on('voice_response_delta', onVoiceDelta);
        window.addEventListener('pagehide', onPageHide);
        // Drafts are only a live rendering aid. A persisted pending bubble means
        // the page/call ended before reconciliation and must not survive reload.
        serial.current = serial.current.catch(() => {}).then(() => discardStaleVoiceDrafts(ctx));
        onChat();
        return () => {
            events.forEach((event) => ctx.eventSource.removeListener(event, onChat));
            wsService.off('voice_message', onVoice);
            wsService.off('voice_response_delta', onVoiceDelta);
            window.removeEventListener('pagehide', onPageHide);
            void hangup();
        };
    }, []);

    useEffect(() => {
        if (!open) return;
        const dialog = panel.current;
        if (!dialog) return;
        if (typeof dialog.showModal === 'function' && !dialog.open) dialog.showModal();
        else if (!dialog.open) dialog.setAttribute('open', '');
        dialog.focus();
        return () => {
            if (typeof dialog.close === 'function' && dialog.open) dialog.close();
        };
    }, [open, onOpenChange]);

    const active = status !== 'idle';
    useEffect(() => {
        onActiveChange(active);
    }, [active, onActiveChange]);

    return <>
        <style>{`
            .rp-voice-panel { pointer-events:auto; position:fixed; inset:0; z-index:2147483000; width:360px; max-width:calc(100vw - 32px); max-height:calc(100dvh - 32px); overflow:auto; box-sizing:border-box; margin:auto; background:#171b25; color:#f0f3fa; border:1px solid #536079; border-radius:20px; box-shadow:0 18px 64px #000a; padding:20px; font:14px/1.5 system-ui,sans-serif; animation:rp-voice-panel-in 180ms cubic-bezier(.22,1,.36,1); }
            .rp-voice-panel::backdrop { background:rgba(5,8,15,.58); backdrop-filter:blur(2px); -webkit-backdrop-filter:blur(2px); }
            .rp-voice-panel header { display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:12px; }
            .rp-voice-panel button { min-height:44px; min-width:44px; border:1px solid #526078; border-radius:12px; background:#293244; color:#fff; cursor:pointer; padding:10px 14px; font:inherit; }
            .rp-voice-panel button:focus-visible { outline:3px solid #8ac9ff; outline-offset:3px; }
            .rp-voice-panel button:disabled { opacity:.5; cursor:wait; }
            .rp-voice-actions { display:flex; gap:10px; margin-top:16px; flex-wrap:wrap; }
            .rp-voice-actions button { flex:1; }
            .rp-voice-panel .rp-voice-end { background:#872f42; border-color:#c9667e; }
            .rp-voice-panel .rp-voice-start { background:#22604d; border-color:#459b80; }
            .rp-voice-text { max-height:110px; overflow:auto; overflow-wrap:anywhere; color:#cbd5e1; margin:12px 0; }
            .rp-voice-error { color:#ffb8c7; overflow-wrap:anywhere; }
            .rp-voice-note { color:#a9b7ca; font-size:12px; margin-top:12px; }
            @keyframes rp-voice-panel-in { from { opacity:0; transform:translateY(12px); } to { opacity:1; transform:none; } }
            @media(max-width:600px) {
                .rp-voice-panel { width:calc(100vw - 16px); max-width:none; max-height:calc(100dvh - 16px); padding:18px 16px 16px; border-radius:22px; }
                .rp-voice-panel::before { content:''; display:block; width:38px; height:4px; margin:-7px auto 12px; border-radius:999px; background:#718096; opacity:.75; }
                .rp-voice-text { max-height:80px; }
            }
            @media(prefers-reduced-motion:reduce) { .rp-voice-panel { animation:none; } }
        `}</style>
        <div ref={audioHost} style={{ display: 'none' }} />
        {open && createPortal(
            <dialog className="rp-voice-panel" aria-label="Hermes voice call" tabIndex={-1} ref={panel}
                onCancel={(event) => { event.preventDefault(); onOpenChange(false); }}
                onPointerDown={(event) => {
                    const rect = event.currentTarget.getBoundingClientRect();
                    const outside = event.clientX < rect.left || event.clientX > rect.right
                        || event.clientY < rect.top || event.clientY > rect.bottom;
                    if (outside) onOpenChange(false);
                }}>
                <header><strong>Hermes voice</strong><button type="button" aria-label="Collapse call panel" onClick={() => onOpenChange(false)}>×</button></header>
                <div role="status">{labels[status] || status}{active && ` · ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`}</div>
                {transcript && <p className="rp-voice-text">{transcript}</p>}
                {error && <p role="alert" className="rp-voice-error">{error}</p>}
                {audioBlocked && <button type="button" onClick={() => current.current?.room.startAudio()}>Enable audio</button>}
                <div className="rp-voice-actions">
                    {!active ? <button type="button" className="rp-voice-start" onClick={start}>Start call</button> : <>
                        <button type="button" disabled={status === 'connecting' || status === 'ending'} aria-pressed={muted} onClick={async () => {
                            try { await current.current?.room.localParticipant.setMicrophoneEnabled(muted); setMuted(!muted); }
                            catch (err) { setError(err.message); }
                        }}>{muted ? 'Enable microphone' : 'Mute microphone'}</button>
                        <button type="button" className="rp-voice-end" disabled={status === 'ending'} onClick={hangup}>Hang up</button>
                    </>}
                </div>
                <div className="rp-voice-note">{active ? 'You can interrupt Hermes by speaking. The panel can be collapsed during the call.' : 'The call uses this chat, its profile, and its model. Voice messages are saved to the chat.'}</div>
            </dialog>,
            document.body,
        )}
    </>;
}
