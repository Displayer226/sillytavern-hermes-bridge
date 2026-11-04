# Development coordination

## Repository and ownership

This is the public bridge repository. `proxy-ST/` (FastAPI/Python) and
`responses-proxy/` (React/SillyTavern) are ordinary directories owned here.
`hermes-agent/` is a separately versioned submodule: preserve its pinned revision
and follow its own instructions when a task explicitly includes it. Do not
replace the fork with upstream or silently copy changes from another workspace.

Read the relevant architecture and contract before cross-component work.
Keep implementation, UI text and shared documentation in English. Communicate
with the user in their language. Never edit generated extension bundles.

## Coordinator and delegation

The primary agent coordinates; a delegated agent follows its narrower role.
For an authorized implementation task, prefer the configured `implementer` role
for bounded changes, then the `reviewer` role for independent review. This is an
explicit request to use native Codex subagents for substantial scoped work.
Do small administrative edits directly when delegation would cost more.

- First establish the latest user objective, request type, Git state and scope.
  A review or diagnosis does not authorize a fix. A backlog is not permission
  to implement every item. A worker's completion is not final acceptance.
- Give each assignment an outcome, permitted files/behaviors, exclusions,
  acceptance criteria, validation commands and a clear stopping point.
- Start with one active delegated task at a time. The two-thread configuration
  allows retaining implementer/reviewer contexts, not parallel writers. Wait
  for implementation to finish before review; do not edit worker-owned files
  concurrently. Workers must not delegate again.
- Use short task-specific context and reports; do not resend entire transcripts.
  Reuse a relevant worker for corrections, but keep the reviewer independent.
  Never silently switch models if a configured role is unavailable.
- Inspect the resulting diff, including untracked files, and relevant evidence.
  Have the implementer address concrete findings, then recheck. After two
  unsuccessful correction cycles, checkpoint and report the unresolved issue
  instead of starting an unbounded retry loop.
- Continue safely within the approved objective; stop at its acceptance boundary.
  Keep planning, security decisions, acceptance and user questions with the
  coordinator. Do not run Hermes/Kanban or another dispatcher alongside this
  workflow unless explicitly requested and coordinated.

## Preserve work and control side effects

Existing changes belong to the user. Inventory them before editing and preserve
unrelated hunks, even in shared files. Do not stash, reset, clean, force-remove a
worktree or switch branches to make a dirty tree disappear. A new worktree sees
committed content, not the current uncommitted candidate; do not review the
wrong baseline. Use `apply_patch` for local edits.

Do not stage, commit, merge, push, open PRs, publish, update submodule pointers,
deploy or recreate services unless the current user request includes that step.
Never read or copy private environment files, credentials, auth storage, session
databases, personal chats, memory or production logs as a default diagnostic.
Do not contact production APIs, providers, Docker daemons or remote hosts for
routine development validation. Local synthetic fixtures are the default.
Static Compose rendering with the explicit public example is not runtime testing.

The public Quick Start remains local, text-only, `default` profile only, with
tools disabled. Preserve server allowlists, bearer authentication and disabled
legacy query-token auth. An advanced OIDC, voice, tool or admin workflow must
not weaken this default. Browser connectivity, backend socket connectivity and
gateway readiness are distinct states. Never retry an ambiguously delivered
prompt or interactive response merely because a transport timed out.

## Validation and evidence

Use the current package scripts and CI as the command reference. Run tests in
isolated temporary state with synthetic credentials, not an inherited deployment
environment. Avoid import-time creation of runtime data in the source tree.

- Extension: unit tests and production build for behavioral changes; targeted
  browser harnesses for UI lifecycle and async state, with temporary build output.
- Proxy: focused tests, Ruff on supported paths and the CI strict mypy targets
  (`schemas.py`, `health.py`). Do not describe those targets as global type safety.
  Prefer the isolated auth launcher for the auth consolidation candidate.
- Installation: pin/provenance checks and Compose configuration are static
  evidence only. Real provider, clean-clone installation and production smoke
  tests require their own explicitly authorized environment and report.

State exact commands, results, unrun checks and limits. Distinguish worker-reported
evidence from checks personally reproduced and remote CI on a committed revision.
Do not lower assertions, change security policy or hide an existing failure to
make a check pass. Keep tokens, raw prompts, reasoning and exception details out
of reports. Do not delete unexpected runtime data; report it for safe handling.

## Stop and resume

Stop and ask one concise question when a material product choice, wider scope,
new authority, credentials, sensitive data, production access or destructive
action is required. Stop delegated work before asking; do not let it continue
making assumptions in the background. Do not ask the user to paste secrets.

At a checkpoint, update an explicitly designated local handoff with the current
objective, candidate state, findings, tests and next action. `.codex/local/` is
ignored for local coordination notes, not secret storage. If a session is asked
to resume from a local handoff, read it and verify its Git facts before acting.
The working agreement and launch procedure are in
[docs/development-workflow.md](docs/development-workflow.md).
