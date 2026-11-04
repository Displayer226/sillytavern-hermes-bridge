# Native Codex coordination

This optional developer workflow replaces manually relaying tasks between editor
windows. It is not part of the bridge runtime, a new service, or a requirement for
contributors using other tools.

## Roles and scope

| Role | Project default | Responsibility |
| --- | --- | --- |
| Primary coordinator | `gpt-6.1-sol`, high | Scope, architecture, decisions, acceptance and user questions. |
| `implementer` | `gpt-6.1-terra`, medium | One assigned patch, regressions and scoped validation. |
| `reviewer` | `gpt-6.1-terra`, medium | Independent review; no source edits. |

Project settings are in `.codex/config.toml`; role definitions are in
`.codex/agents/`. `AGENTS.md` defines the working agreement. The two-child-thread
limit permits keeping both role contexts for follow-ups. Only one delegated
task should be active at a time initially; it is not permission for two writers.
The coordinator remains responsible for the security-sensitive acceptance.

These are defaults, not proof of model availability or a quota saving. Check
models available to the signed-in account. Multiple agents consume additional
tokens; measure usage before expanding concurrency. Do not introduce API billing
or change the account/provider just to make a role start.

## Start in the IDE

1. Open this repository as the project, not its parent containing other checkouts.
2. Finish or stop any previous writer before starting a new coordinating session.
3. Use the normal project trust flow if needed; do not bypass policy. Start a new
   Codex chat and check the primary model/reasoning selection. An explicit client
   selection may override the project default.
4. Ask the coordinator to read `AGENTS.md`, this guide and any explicitly supplied
   local handoff. Have it verify Git facts and the available delegation tools.
5. First delegate one read-only inventory using `reviewer`, then collect its
   result. Check the selected role/model where the client exposes it. If role
   discovery or model access fails, report the exact blocker; do not silently run
   the whole task on the primary model or change the account.
6. After this smoke test, continue only the bounded objective requested by the
   user. Keep implementation and review sequential, with concise progress updates.

A suitable generic first request is:

> Read AGENTS.md and docs/development-workflow.md. Act as coordinator. Verify the
> project state, then use the reviewer for one read-only delegation smoke test.
> Report the actual role/model evidence and any limitations. Continue only the
> specific objective I assign; no commits, publication or deployment.

Use the existing Codex login. This workflow does not configure an API key,
provider, sandbox, approval policy, MCP server, or global settings. Custom agents
inherit the parent's permissions: a review-only instruction is not a technical
read-only sandbox. Review permission settings before running a session.

## Task and result format

A worker assignment should contain:

- Request type and objective: review, diagnose, or implement.
- Current candidate/base and existing changes to preserve.
- Permitted paths/behaviors and excluded components/operations.
- Expected behavior, acceptance tests, test isolation and stopping condition.

A result should contain the outcome, changed files, findings with evidence,
exact checks/results, checks not run and remaining decisions. Do not request
complete transcripts or raw runtime logs. Review reports lead with findings,
not an unsupported claim that the whole release is ready.

The coordinator may continue implementation/review/corrections inside an approved
objective. A missing product decision, unsafe test environment or material scope
expansion requires a checkpoint and a user question, not an invented permission.
Stop at the approved boundary even if the backlog still has work.

## Local handoff and limits

An optional `.codex/local/coordination-state.md` can retain session-specific work
state. This directory is ignored by Git. Do not put secrets there. Keep public
guidance generic: personal paths, deployments and local reports do not belong in
the shared workflow. Verify handoff facts on every resume; notes can become stale.

Native subagents remove manual task relaying within an active Codex session.
These instructions do not install an unattended scheduler or guarantee work
continues after the client closes. A strict durable pause/resume service would
be a separate project; do not add one as part of a bridge feature.

## References

- [Codex subagents and custom roles](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [Project configuration and precedence](https://learn.chatgpt.com/docs/config-file/config-basic)

Configuration was prepared against the local CLI and IDE versions recorded in
the local handoff. A static check is not a successful delegated model run.
