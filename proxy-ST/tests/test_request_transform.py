from __future__ import annotations

from proxy_st.request_transform import (
    hermes_image_attachments_from_messages,
    hermes_prompt_payload_from_messages_for_request,
    hermes_prompt_from_messages,
    hermes_prompt_from_messages_for_request,
    hermes_undo_before_submit_reason,
    proxy_participant_name,
)


def test_proxy_participant_name_normalizes_untrusted_metadata() -> None:
    body = {
        "st_proxy": {
            "user_name": "  Example User\nInjected heading  ",
            "character_name": "Hermes",
        }
    }

    assert proxy_participant_name(body, "user_name") == "Example User Injected heading"
    assert proxy_participant_name(body, "character_name") == "Hermes"
    assert proxy_participant_name(body, "unsupported") is None


def test_hermes_image_attachments_use_latest_user_turn_only() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "old image"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,b2xk"}},
            ],
        },
        {"role": "assistant", "content": "ok"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "new image"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,bmV3"}},
            ],
        },
    ]

    attachments = hermes_image_attachments_from_messages(messages)

    assert attachments == [
        {
            "content_base64": "data:image/jpeg;base64,bmV3",
            "filename": "sillytavern_image_1.jpg",
            "mime_type": "image/jpeg",
        }
    ]
    prompt = hermes_prompt_from_messages(messages)
    assert "new image" in prompt
    assert "data:image" not in prompt


def test_hermes_image_attachments_do_not_reuse_past_image_turn() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "old image"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,b2xk"}},
            ],
        },
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "plain text follow-up"},
    ]

    attachments = hermes_image_attachments_from_messages(messages)

    assert attachments == []


def test_hermes_image_attachments_survive_later_user_control_prompt() -> None:
    messages = [
        {"role": "assistant", "content": "hi"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "current image"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,Y3VycmVudA=="}},
            ],
        },
        {"role": "user", "content": "post-history instruction"},
    ]

    attachments = hermes_image_attachments_from_messages(messages)

    assert attachments == [
        {
            "content_base64": "data:image/png;base64,Y3VycmVudA==",
            "filename": "sillytavern_image_1.png",
            "mime_type": "image/png",
        }
    ]


def test_hermes_image_attachments_detect_image_only_turn() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/webp;base64,aW1hZ2U="}},
            ],
        },
    ]

    attachments = hermes_image_attachments_from_messages(messages)

    assert attachments[0]["filename"] == "sillytavern_image_1.webp"
    assert attachments[0]["content_base64"] == "data:image/webp;base64,aW1hZ2U="


def test_hermes_undo_before_submit_reason_for_retry_generation_types() -> None:
    assert hermes_undo_before_submit_reason({"type": "regenerate"}) == "regenerate"
    assert hermes_undo_before_submit_reason({"type": "swipe"}) == "swipe"
    assert hermes_undo_before_submit_reason({"type": "normal"}) is None


def test_hermes_undo_before_submit_reason_from_metadata() -> None:
    assert hermes_undo_before_submit_reason({
        "st_proxy": {"hermes_undo_before_submit": "selected_swipe:2:0:normal"}
    }) == "selected_swipe:2:0:normal"
    assert hermes_undo_before_submit_reason({
        "st_proxy": {"hermes_undo_before_submit": False}
    }) is None


def test_hermes_active_session_prompt_uses_latest_user_delta() -> None:
    messages = [
        {"role": "system", "content": "Stay in character."},
        {"role": "user", "content": "old user turn"},
        {"role": "assistant", "content": "old assistant turn"},
        {"role": "user", "content": "continue from here"},
    ]

    prompt = hermes_prompt_from_messages_for_request(messages, active_session=True)

    assert prompt == "continue from here"


def test_hermes_fresh_session_prompt_bootstraps_full_context() -> None:
    messages = [
        {"role": "system", "content": "Stay in character."},
        {"role": "user", "content": "old user turn"},
        {"role": "assistant", "content": "old assistant turn"},
        {"role": "user", "content": "continue from here"},
    ]

    prompt = hermes_prompt_from_messages_for_request(messages, active_session=False)

    assert "[SillyTavern persona and instructions]" in prompt
    assert "[SillyTavern conversation so far]" in prompt
    assert "old assistant turn" in prompt
    assert "continue from here" in prompt


def test_hermes_structured_payload_separates_instructions_history_and_user_text() -> None:
    messages = [
        {"role": "system", "content": "Stay in character."},
        {"role": "user", "content": "old user turn"},
        {"role": "assistant", "content": "old assistant turn"},
        {"role": "developer", "content": "Answer as the active persona."},
        {"role": "user", "content": "continue from here"},
    ]

    payload = hermes_prompt_payload_from_messages_for_request(
        messages,
        active_session=False,
        integration_context="Use native MCP tools.",
    )

    assert payload.text == "continue from here"
    assert payload.conversation_history == [
        {"role": "user", "content": "old user turn"},
        {"role": "assistant", "content": "old assistant turn"},
    ]
    assert payload.system_context is not None
    assert "Use native MCP tools." in payload.system_context
    assert "Stay in character." not in payload.system_context
    assert "Answer as the active persona." not in payload.system_context
    assert payload.persona_context == "Stay in character.\n\nAnswer as the active persona."
    assert payload.persona_version is not None
    assert payload.persona_reminder is not None
    assert "[SillyTavern persona reminder]" in payload.persona_reminder
    assert "Stay in character." in payload.persona_reminder
    assert "both the USER persona/profile and the ASSISTANT character card" in payload.persona_reminder
    assert "Never adopt the user's name" in payload.persona_reminder


def test_hermes_structured_payload_uses_delta_for_active_session() -> None:
    messages = [
        {"role": "system", "content": "Stay in character."},
        {"role": "user", "content": "old user turn"},
        {"role": "assistant", "content": "old assistant turn"},
        {"role": "user", "content": "continue from here"},
    ]

    payload = hermes_prompt_payload_from_messages_for_request(
        messages,
        active_session=True,
    )

    assert payload.text == "continue from here"
    assert payload.conversation_history is None
    assert payload.system_context is not None
    assert "Stay in character." not in payload.system_context
    assert payload.persona_context == "Stay in character."
    assert payload.persona_reminder is not None
    assert "Stay in character." in payload.persona_reminder


def test_hermes_structured_payload_sends_empty_persona_clear() -> None:
    payload = hermes_prompt_payload_from_messages_for_request(
        [{"role": "user", "content": "hello"}],
        active_session=True,
    )

    assert payload.text == "hello"
    assert payload.persona_context == ""
    assert payload.persona_reminder == ""
    assert payload.persona_version == ""


def test_hermes_retry_prompt_uses_delta_for_active_session() -> None:
    messages = [
        {"role": "system", "content": "Stay in character."},
        {"role": "user", "content": "retry me"},
        {"role": "assistant", "content": "previous failed reply"},
    ]

    prompt = hermes_prompt_from_messages_for_request(
        messages,
        sync_reason="swipe",
        active_session=True,
    )

    assert prompt == "retry me"


def test_hermes_selected_swipe_prompt_keeps_authoritative_context() -> None:
    messages = [
        {"role": "system", "content": "Stay in character."},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "selected branch reply"},
        {"role": "user", "content": "next after branch"},
    ]

    prompt = hermes_prompt_from_messages_for_request(
        messages,
        sync_reason="selected_swipe:2:1:normal",
        active_session=True,
    )

    assert "[SillyTavern conversation so far]" in prompt
    assert "selected branch reply" in prompt
    assert "next after branch" in prompt
