# Semi-RP assistant prompts for the Hermes bridge

Use [SillyTavern Hermes - Semi-RP.json](../presets/SillyTavern%20Hermes%20-%20Semi-RP.json) as an instructions-only Chat Completion preset. It contains no user identity, character card, model-provider credentials, host paths, or endpoint URLs. The three prompts are in English; replies follow the latest user's language unless explicitly requested otherwise.

## Install

Import the JSON using the import button in SillyTavern's AI Response Configuration / Chat Completion presets, then select it. Keep your existing proxy connection configuration; the file deliberately omits connection and sampling settings. It provides prompts, their order, and the standard new-chat marker. Configure the bridge connection separately using the installation guide.

Enable Main Prompt, Auxiliary Prompt, and Post-History Instructions. Use the System role and relative position: Main first, Auxiliary before Chat History, and Post-History last. Keep Character Description, Personality, Scenario, and the user's Persona Description enabled as needed. Enhance Definitions is disabled so the model is not prompted to invent assistant lore. Existing character-card overrides may replace Main or Post-History; preserve the global text with `{{original}}` or disable those overrides when testing this preset.

The identifiers `main`, `nsfw`, and `jailbreak` are SillyTavern's existing identifiers for these three slots. Their historical names do not require adult content or jailbreak instructions. Auxiliary is an optional additional system prompt; its subject is a design choice, not a special API privilege.

## Responsibilities and delivery

| Layer | Put here | Delivery to Hermes |
| --- | --- | --- |
| Main (`main`) | Assistant identity comes from the character card; distinguish the user persona; useful semi-RP behavior, factual grounding, practical work, response style. | Combined with the other pre-history system text in `persona_context` when the mapped session is created. |
| Auxiliary (`nsfw`) | SillyTavern/Hermes/bridge awareness, meaning of supplied context, conditional UI features, access limits, truthful tool outcomes. | Also in `persona_context`, before history. |
| Post-History (`jailbreak`) | A short reminder for the current reply: latest request, continuity, user language, concrete progress. | Appended to the submitted text under `[SillyTavern post-history instructions]` on each turn. |
| Proxy integration context | Current chat id, participant names, selected workspace mapping, native tool usage and conditional character-edit guidance. | Stable `system_context` attached at session creation. Never hardcode these values in a shared preset. |
| Proxy response contract | Visible history is authoritative; continue without repeating the opening; use real tools; follow the current user's language. | Part of stable `system_context`. This protects the bridge even when another preset is selected. |
| Proxy persona reminder | Distinguish assistant card from user profile. | A compact reference to the full `persona_context`, not another copy of its text. |

The runtime prepends its own Hermes system instructions and then attaches these stable bridge fields to the agent system prompt. The full character/user context is kept once; the reminder does not truncate or replace it. Active requests send the current text rather than replaying all visible history. New or expired mappings are seeded from visible SillyTavern history. A changed stable context, character, profile, or workspace can rebuild the mapping. Exact per-turn routing remains the proxy's responsibility, not a task for the model.

The split depends on the standard `[Start a new Chat]` marker: system/developer messages before it become pre-history context, and those after it become post-history instructions. Pre-marker user/assistant examples and named `example_user`/`example_assistant` system messages are retained there with explicit example-role labels; they are illustrative, not real conversation history. Only post-marker conversation supplies the current user turn and attachments. If it contains no user turn, the bridge returns its empty-input error instead of submitting an example, greeting, or post-history instruction. Without the marker, the transformer retains its legacy routing and treats all system text as pre-history context. This preset preserves the marker for solo chats. Group and unusual prompt layouts require separate validation.

Voice snapshots label the assistant character card fields and user persona separately before sending them as stable persona context.

Post-History gets a final reminder close to the request; it does not acquire a higher system privilege merely by being later. The bridge sends it as labeled text within the submitted user turn rather than as an independent native system message. Avoid placing the entire operating manual there.

## Capabilities and boundaries

Character cards define how the assistant speaks; they do not grant browser control, tools, or filesystem access. The user persona and Hermes USER.md describe the user. World information, examples, and supplied documents provide context, not evidence that a real action occurred.

The extension can expose tool output, approvals/clarifications, model/profile/workspace selection, and optional voice/session controls. Availability depends on the deployment. The public text-only Quick Start disables tools and does not configure voice or a host workspace. The same prompts still work there because they only promise capabilities actually present in the runtime tool list.

Character-description editing requires the native integration tool and its result. A pending proposal is not an applied edit. The proxy no longer claims this MCP tool is always registered, or suggests a machine-specific localhost HTTP fallback. Tool names and argument details remain in runtime guidance and tool schemas rather than being duplicated in the preset. Current runtime integration context and observed results take precedence over older skills, memories, or documentation for capabilities and configuration; conflicting descriptions should be verified or reported rather than merged.

Language follows the latest actual user text, including a switch from French to English in an ongoing chat. English instructions, an English card, earlier French history, or a stored preference must not force a different reply language. The small language reminder is intentionally shared between Post-History and the proxy contract.

## Verify and customize

Start a disposable chat with a synthetic character card and a distinct user persona. Check assistant identity, a casual exchange, a practical request, a language switch in both directions, and a request for an unavailable UI action. Confirm that the assistant remains useful and does not claim imaginary actions. Inspect the outgoing prompt using SillyTavern's Prompt Itemization and, for runtime routing, proxy logs/tests.

Put personal biography and detailed voice/style in the character card or user persona, not in these general prompts. Load large domain instructions or lore only when relevant rather than expanding these three blocks. Editing a preset on disk requires reloading the SillyTavern page before using it; an open page still holds its previous prompts.

## Validation

On 2026-10-05, proxy checks passed: 192 tests in the development integration and 219 tests in the public export, plus Ruff and the development strict mypy targets. The deployed proxy health check reported both Hermes services active and its WebSocket ready. Disposable live checks covered character/user identity separation, switching reply language, and acknowledging an unavailable theme-setting action. A stale local architecture skill was identified as a conflicting source and removed from the local active skill directories with an external backup; no personal skill content is included in this shared preset.

These checks cover this local integration/model, not every model or installation. Prompt compliance is not an API guarantee. The full preset remains separate from personal memories and skills.

## Sources

- [SillyTavern prompt building](https://docs.sillytavern.app/usage/prompts/): Main defines general behavior; Post-History provides final response guidance.
- [SillyTavern Prompt Manager](https://docs.sillytavern.app/usage/prompts/prompt-manager/): roles, order, triggers, relative/in-chat positions, and saving presets.
- [SillyTavern macros](https://docs.sillytavern.app/usage/core-concepts/macros/): character overrides and `{{original}}`.
- Implementation: `proxy_st/request_transform.py`, `proxy_st/relay.py`, `proxy_st/hermes_ws.py`, and the pinned Hermes `tui_gateway/server.py`.
