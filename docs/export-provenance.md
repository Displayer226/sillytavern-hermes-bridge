# Exported component provenance

This repository is a curated public export. The source revision IDs below identify the selected upstream inputs; this export is **not byte-for-byte identical** to those source trees. It applies the relevant proxy and Compose changes while retaining export-specific documentation, examples, metadata, ignore rules, and public deployment wiring.

The proxy candidate `6a3fe191bd373a28e02772350ceb769f124ec796` and Compose candidate `3ed61261c7fe28cffe5b959ed8e1d8d81c825a6b` existed only in local source repositories. They were not published and are not publicly fetchable references. They document the inputs used to prepare this export; the initial commit of this export is the public reference for its contents.

| Component | Source revision | How it is represented here |
| --- | --- | --- |
| Session proxy | `6a3fe191bd373a28e02772350ceb769f124ec796` | The profile allowlist changes are transposed into `proxy-ST/` as a targeted patch. Existing export README, `.dockerignore`, `.gitignore`, generic configuration, and other export changes remain in place. |
| SillyTavern extension | `1055955db5d51c6d40b624ee9fbeb68a4008296a` | Extension behavior and tests use this source revision, with public-facing README and package/manifest metadata adapted for this export. |
| Hermes Agent | `5661709c997cb5557cc337fd428b44c598ab43ca` | Kept as a true Git submodule at `hermes-agent/`, pinned to this exact commit; Compose uses the same revision for the Hermes image build argument. |
| Public Compose profile pin | `3ed61261c7fe28cffe5b959ed8e1d8d81c825a6b` | The relevant `HERMES_PROFILE_ALLOWLIST=default` setting is applied to this export's `compose.public.yaml`; the source repository's submodule gitlink is replaced by the pinned public Hermes gitlink above. |

## Export adaptations

- The top-level README, Quick Start, architecture guide, examples, and environment template describe a generic local deployment and use the public repository URL rather than private deployment details.
- The extension manifest uses a generic maintainer name and the public repository URL. Its npm package metadata identifies the exported package as `responses-proxy` under `AGPL-3.0-only`.
- `proxy-ST/README.md` describes the companion public setup. Its `.dockerignore` admits only the proxy build inputs, while `.gitignore` excludes environment files, logs, bytecode, and runtime data.
- `compose.public.yaml` contains the export's loopback-only ingress and network layout in addition to the selected profile allowlist setting.
- No credentials, session databases, VM data, or private runtime files are included.
- The root-repository secret-scan scope and the separate Hermes audit are documented in [the secret-scan audit note](secret-scan-audit.md). Hermes findings are not treated as resolved by the root CI scan.

The proxy export was not replaced by a source checkout. Relative to the selected proxy revision, it omits `.env.example`, `.github/workflows/backend-ci.yml`, and `compose.yaml`, and adds its own `.dockerignore`. Existing export-side differences remain in `README.md`, `main.py`, `proxy_st/__init__.py`, `proxy_st/dummy.py`, `proxy_st/rate_limit.py`, `proxy_st/relay.py`, `proxy_st/request_transform.py`, `proxy_st/state.py`, `tests/test_hermes_auth.py`, `tests/test_hermes_ws.py`, `tests/test_integration.py`, and `tests/test_relay.py`; the profile-policy hunks from the selected candidate were applied on top of them.

The extension export differs from revision `1055955db5d51c6d40b624ee9fbeb68a4008296a` in its README, manifest, npm package metadata, and whitespace-only formatting in `src/ToolCallsPanel.js`. Its public instructions and metadata are retained rather than copied over from the source checkout.

The proxy patch changes profile configuration and policy, request/API enforcement, session restoration and revocation handling, profile option filtering, and corresponding tests. The allowlist is optional: when `HERMES_PROFILE_ALLOWLIST` is unset, profile selection keeps the proxy's prior unrestricted behavior.
