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
    assert "Stay in character." not in payload.persona_reminder
    assert "ASSISTANT character card" in payload.persona_reminder
    assert "never adopt the user's identity" in payload.persona_reminder


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
    assert "Stay in character." not in payload.persona_reminder


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


def test_reply_language_uses_current_user_turn_despite_english_post_history() -> None:
    messages = [
        {"role": "system", "content": "Character: helpful. User profile: French speaker."},
        {"role": "system", "content": "[Start a new Chat]"},
        {"role": "user", "content": "Salut"},
        {"role": "assistant", "content": "Bonjour !"},
        {"role": "user", "content": "Hello what's up"},
        {"role": "system", "content": "Answer the latest request in character."},
    ]
    for active in (False, True):
        payload = hermes_prompt_payload_from_messages_for_request(messages, active_session=active)
        assert payload.text.startswith("Hello what's up\n\n[SillyTavern post-history instructions]")
        assert "Answer the latest request in character." in payload.text
        assert "Answer the latest request in character." not in payload.persona_context
        assert "latest actual user message" in payload.system_context
        assert "even after French history" in payload.system_context
    assert "latest actual user message" in hermes_prompt_from_messages(messages)


def test_persona_reminder_does_not_duplicate_large_context() -> None:
    context = "Character card: helpful. " + "Large world info. " * 1000
    payload = hermes_prompt_payload_from_messages_for_request([
        {"role": "system", "content": context},
        {"role": "user", "content": "Hi"},
    ])
    assert payload.persona_context == context.strip()
    assert len(payload.persona_reminder) < 500
    assert "Large world info." not in payload.persona_reminder


def test_pre_marker_examples_are_labeled_stable_persona_context() -> None:
    messages = [
        {"role": "system", "content": "Character card"},
        {"role": "user", "content": "example question"},
        {"role": "assistant", "content": "example answer"},
        {"role": "system", "name": "example_user", "content": "named question"},
        {"role": "system", "name": "example_assistant", "content": "named answer"},
        {"role": "system", "content": "[Start a new Chat]"},
        {"role": "user", "content": "real earlier"},
        {"role": "assistant", "content": "real reply"},
        {"role": "user", "content": "real current"},
    ]
    fresh = hermes_prompt_payload_from_messages_for_request(messages)
    active = hermes_prompt_payload_from_messages_for_request(messages, active_session=True)
    assert fresh.persona_context == active.persona_context
    assert fresh.persona_context is not None
    for label, content in [("EXAMPLE USER", "example question"), ("EXAMPLE ASSISTANT", "example answer"),
                           ("EXAMPLE USER", "named question"), ("EXAMPLE ASSISTANT", "named answer")]:
        assert f"[{label}]\n{content}" in fresh.persona_context
        assert fresh.persona_context.count(content) == 1
        assert content not in str(fresh.conversation_history)
        assert content not in fresh.text
    assert fresh.conversation_history == [
        {"role": "user", "content": "real earlier"}, {"role": "assistant", "content": "real reply"},
    ]
    assert active.conversation_history is None
    assert fresh.text == active.text == "real current"
    changed = [dict(message) for message in messages]
    changed[1]["content"] = "changed example"
    assert hermes_prompt_payload_from_messages_for_request(changed).persona_version != fresh.persona_version


def test_marker_without_real_user_never_submits_example_greeting_or_post_instruction() -> None:
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "example"},
                                      {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}}]},
        {"role": "assistant", "content": "example answer"},
        {"role": "system", "content": "[Start a new Chat]"},
        {"role": "assistant", "content": "greeting"},
        {"role": "system", "content": "post instruction"},
    ]
    for active in (False, True):
        payload = hermes_prompt_payload_from_messages_for_request(messages, active_session=active)
        assert payload.text == ""
        assert hermes_image_attachments_from_messages(messages) == []


def test_marker_current_user_and_images_exclude_examples_and_keep_swipe() -> None:
    messages = [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}}]},
        {"role": "system", "content": "[Start a new Chat]"},
        {"role": "user", "content": "actual"},
        {"role": "assistant", "content": "swiped reply"},
    ]
    payload = hermes_prompt_payload_from_messages_for_request(messages, sync_reason="swipe", active_session=True)
    assert payload.text == "actual"
    assert hermes_image_attachments_from_messages(messages) == []


def test_no_marker_preserves_legacy_assistant_fallback() -> None:
    payload = hermes_prompt_payload_from_messages_for_request([
        {"role": "system", "content": "instruction"}, {"role": "assistant", "content": "legacy greeting"},
    ])
    assert payload.text == "legacy greeting"
    assert payload.persona_context == "instruction"


def test_multiple_markers_keep_only_first_area_examples_and_last_conversation() -> None:
    messages = [
        {"role": "user", "content": "genuine example user"},
        {"role": "assistant", "content": "genuine example assistant"},
        {"role": "system", "content": "[Start a new Chat]"},
        {"role": "user", "content": "old actual user"},
        {"role": "assistant", "content": "old actual assistant"},
        {"role": "system", "name": "example_user", "content": "old named user"},
        {"role": "system", "name": "example_assistant", "content": "old named assistant"},
        {"role": "system", "content": "ordinary system instruction"},
        {"role": "system", "content": "[Start a new chat]"},
        {"role": "user", "content": "new current user"},
    ]
    for active in (False, True):
        payload = hermes_prompt_payload_from_messages_for_request(messages, active_session=active)
        assert payload.persona_context is not None
        assert "[EXAMPLE USER]\ngenuine example user" in payload.persona_context
        assert "[EXAMPLE ASSISTANT]\ngenuine example assistant" in payload.persona_context
        assert "ordinary system instruction" in payload.persona_context
        for old in ("old actual user", "old actual assistant", "old named user", "old named assistant"):
            assert old not in payload.persona_context
            assert old not in str(payload.conversation_history)
            assert old not in payload.text
        assert payload.conversation_history is None
        assert payload.text == "new current user"
