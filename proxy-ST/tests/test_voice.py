import json
from unittest.mock import AsyncMock

import httpx
import pytest

from proxy_st import voice
from proxy_st.app import app, hermes_ws_manager
from proxy_st.responses import chat_completion_chunk


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def clean_calls(monkeypatch):
    voice.calls.clear()
    monkeypatch.setattr(voice, "WS_TOKEN", "browser-secret")
    monkeypatch.setattr(hermes_ws_manager, "interrupt_session", AsyncMock())
    monkeypatch.setattr(hermes_ws_manager, "close_session", AsyncMock())
    yield
    voice.calls.clear()


def seed_call():
    call = voice.Call("call1", "worker-secret", voice.StartCall(session_id="chat1", profile="local", model="gemma4-12b"))
    voice.calls[call.id] = call
    return call


def test_tool_status_phrases_are_varied_and_voice_friendly():
    assert len(voice.TOOL_STATUS_PHRASES) == 10
    assert len(set(voice.TOOL_STATUS_PHRASES)) == 10
    assert all(phrase.endswith((". ", "… ")) for phrase in voice.TOOL_STATUS_PHRASES)


def test_voice_flush_chunk_is_an_out_of_band_tts_control():
    chunk = chat_completion_chunk(
        "chatcmpl-test", 1, "test", {"voice_flush": True}
    )
    payload = json.loads(chunk.removeprefix("data:").strip())

    assert payload["choices"][0]["delta"] == {"voice_flush": True}
    assert "content" not in payload["choices"][0]["delta"]


@pytest.mark.anyio
async def test_call_tokens_are_scoped_and_browser_routes_require_auth():
    seed_call()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/v1/voice/capabilities")).status_code == 401
        assert (await client.get("/v1/voice/calls/call1/context", headers={"Authorization": "Bearer browser-secret"})).status_code == 401
        result = await client.get("/v1/voice/calls/call1/context", headers={"Authorization": "Bearer worker-secret"})
        assert result.status_code == 200
        assert result.json()["session_id"] == "chat1"
        assert "token" not in result.json()


@pytest.mark.anyio
async def test_events_deduplicate_and_interruption_requires_rebuild():
    call = seed_call()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        event = {"id": "m1", "role": "assistant", "text": "I can", "interrupted": True}
        for _ in range(2):
            result = await client.post("/v1/voice/calls/call1/events", json=event, headers={"Authorization": "Bearer worker-secret"})
            assert result.status_code == 200
        assert call.rebuild
        assert len(call.events) == 1
        events = await client.get("/v1/voice/calls/call1/events?after=1", headers={"Authorization": "Bearer browser-secret"})
        assert events.json()["events"] == []


@pytest.mark.anyio
async def test_hangup_without_first_turn_is_idempotent_and_preserves_events():
    call = seed_call()
    hermes_ws_manager.interrupt_session.side_effect = ValueError("no mapping")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.delete("/v1/voice/calls/call1", headers={"Authorization": "Bearer browser-secret"})
        assert result.status_code == 200
        assert call.closed
        assert not voice.active_call("chat1")
        context = await client.get("/v1/voice/calls/call1/context", headers={"Authorization": "Bearer worker-secret"})
        assert context.status_code == 410
        again = await client.delete("/v1/voice/calls/call1", headers={"Authorization": "Bearer browser-secret"})
        assert again.status_code == 200
        hermes_ws_manager.close_session.assert_awaited_once_with("chat1")


@pytest.mark.anyio
async def test_stream_uses_pinned_routing_and_releases_lock(monkeypatch):
    from fastapi.responses import StreamingResponse
    call = seed_call()
    call.rebuild = True

    async def chunks():
        yield 'data: {"choices":[{"delta":{"content":"Salut"}}]}\n\n'
    forward = AsyncMock(return_value=StreamingResponse(chunks()))
    monkeypatch.setattr(voice, "forward_hermes_streaming", forward)
    broadcast = AsyncMock()
    monkeypatch.setattr(voice, "ws_broadcast", broadcast)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post("/v1/voice/calls/call1/completions", json={"messages": [{"role": "user", "content": "Salut"}]}, headers={"Authorization": "Bearer worker-secret"})
        assert result.status_code == 200
        assert "Salut" in result.text
        assert not call.lock.locked()
        assert not call.rebuild
        hermes_ws_manager.close_session.assert_awaited_once_with("chat1")
        assert forward.call_args.args[0]["st_proxy"]["profile"] == "local"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("model", "expected_prompt_model"),
    [("", None), ("provider/voice-model", "provider/voice-model")],
)
async def test_voice_model_selection_reaches_hermes_without_inventing_a_default(
    monkeypatch, model, expected_prompt_model
):
    import proxy_st.app as app_module
    from proxy_st import relay

    class RelayManager:
        is_connected = True

        def __init__(self):
            self.prompt_models = []
            self.model_switches = []

        def has_prompt_history(self, _session_id):
            return True

        async def ensure_session(self, *_args, **_kwargs):
            return "tui-1"

        async def set_model(self, _session_id, selected_model, **_kwargs):
            self.model_switches.append(selected_model)
            return {"key": "model", "value": selected_model}

        async def submit_prompt(self, _session_id, _text, _images, **kwargs):
            self.prompt_models.append(kwargs["model"])
            yield {"type": "text", "text": "Voice reply."}
            yield {"type": "done", "status": "complete"}

    manager = RelayManager()
    monkeypatch.setattr(app_module, "hermes_ws_manager", manager)
    monkeypatch.setattr(relay, "workspace_cwd_for_selection", lambda _selection: None)
    monkeypatch.setattr(relay, "ws_broadcast", AsyncMock())
    monkeypatch.setattr(relay, "update_agent_status", lambda _session_id, status: status or {"active": False})
    monkeypatch.setattr(voice, "ws_broadcast", AsyncMock())

    start_kwargs = {"model": model} if model else {}
    call = voice.Call(
        "call1",
        "worker-secret",
        voice.StartCall(session_id="chat1", profile="local", **start_kwargs),
    )
    voice.calls[call.id] = call

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/voice/calls/call1/completions",
            json={"messages": [{"role": "user", "content": "Continue"}]},
            headers={"Authorization": "Bearer worker-secret"},
        )

    assert response.status_code == 200, response.text
    assert "Voice reply." in response.text
    assert manager.prompt_models == [expected_prompt_model]
    assert manager.model_switches == ([model] if model else [])


@pytest.mark.anyio
async def test_stream_broadcasts_text_deltas_to_sillytavern(monkeypatch):
    from fastapi.responses import StreamingResponse

    seed_call()
    broadcast = AsyncMock()
    monkeypatch.setattr(voice, "ws_broadcast", broadcast)

    async def forward(_payload, _session_id, _manager, *, text_delta_callback=None, **_kwargs):
        async def chunks():
            await text_delta_callback("Bonjour ")
            yield 'data: {"choices":[{"delta":{"content":"Bonjour "}}]}\n\n'
            await text_delta_callback("Example User")
            yield 'data: {"choices":[{"delta":{"content":"Example User"}}]}\n\n'

        return StreamingResponse(chunks())

    monkeypatch.setattr(voice, "forward_hermes_streaming", forward)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post(
            "/v1/voice/calls/call1/completions",
            json={"messages": [{"role": "user", "content": "Salut"}]},
            headers={"Authorization": "Bearer worker-secret"},
        )

    assert result.status_code == 200
    events = [call.args[1] for call in broadcast.await_args_list]
    deltas = [event for event in events if event["type"] == "voice_response_delta"]
    assert [event["text"] for event in deltas] == ["Bonjour ", "Example User"]
    assert len({event["turn_id"] for event in deltas}) == 1
    assert all(event["call_id"] == "call1" for event in deltas)


@pytest.mark.anyio
async def test_stream_supplies_rotating_tool_status_callback(monkeypatch):
    from fastapi.responses import StreamingResponse

    seed_call()
    monkeypatch.setattr(voice, "TOOL_STATUS_MIN_INTERVAL", 0.0)

    async def forward(
        _payload,
        _session_id,
        _manager,
        *,
        tool_status_callback=None,
        **_kwargs,
    ):
        async def chunks():
            first = await tool_status_callback({"name": "terminal"})
            second = await tool_status_callback({"name": "terminal"})
            yield f'data: {{"first": {json.dumps(first)}, "second": {json.dumps(second)}}}\n\n'

        return StreamingResponse(chunks())

    monkeypatch.setattr(voice, "forward_hermes_streaming", forward)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post(
            "/v1/voice/calls/call1/completions",
            json={"messages": [{"role": "user", "content": "Vérifie"}]},
            headers={"Authorization": "Bearer worker-secret"},
        )

    payload = json.loads(result.text.removeprefix("data:").strip())
    assert payload["first"] in voice.TOOL_STATUS_PHRASES
    assert payload["second"] in voice.TOOL_STATUS_PHRASES
    assert payload["first"] != payload["second"]


@pytest.mark.anyio
async def test_dispatch_reserves_session_and_does_not_return_worker_secret(monkeypatch):
    monkeypatch.setenv("VOICE_TOKEN_URL", "http://voice-web/api/token")
    original_client = httpx.AsyncClient
    broker = AsyncMock()
    broker.post.return_value = httpx.Response(200, json={"serverUrl": "wss://voice", "participantToken": "jwt", "roomName": "room"}, request=httpx.Request("POST", "http://voice-web/api/token"))
    factory = AsyncMock()
    factory.__aenter__.return_value = broker
    async with original_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        monkeypatch.setattr(voice.httpx, "AsyncClient", lambda **kwargs: factory)
        result = await client.post("/v1/voice/calls", json={"session_id": "chat1"}, headers={"Authorization": "Bearer browser-secret"})
        assert result.status_code == 200
        metadata = json.loads(broker.post.call_args.kwargs["json"]["room_config"]["agents"][0]["metadata"])
        assert metadata["proxy_call_id"] == result.json()["callId"]
        assert metadata["proxy_call_token"] not in result.text
        again = await client.post("/v1/voice/calls", json={"session_id": "chat1"}, headers={"Authorization": "Bearer browser-secret"})
        assert again.status_code == 409
