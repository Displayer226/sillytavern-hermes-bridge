# Public Docker Quick Start — local text setup

This guide configures one local SillyTavern-to-Hermes text path. It uses the pinned Hermes source commit recorded in this repository, a local proxy build, and the companion extension. The stack binds its only host port to loopback. The proxy allows only Hermes's `default` profile in this path: it rejects explicit non-default selections sent through its HTTP or WebSocket APIs, and discards a saved non-default selection before restoring a session so the session uses `default`. It does not configure tools, a host workspace, voice, remote access, or multi-user use.

**Validation status:** on 2026-09-25, the text-only conversation and proxy-only restart flow passed in a disposable VM using Docker Engine 29.8.1 and Compose 5.5.1. This run used a fresh local Git clone created from a local Git bundle, at super-project commit `97c73bd9639a42b1f0d80b82c9b77bdbfe9b67e3` with Hermes submodule `5661709c997cb5557cc337fd428b44c598ab43ca`. It has not yet been tested from an anonymous GitHub clone; replace the repository owner placeholder and rerun this guide from an anonymous clone before publication.

## Requirements

- Linux with Docker Engine and Docker Compose v2.33.1 or newer.
- Git with submodule support; Node.js and npm for building the extension.
- SillyTavern available to the browser on the same host.
- A model-provider account and API key accepted by Hermes, or a reachable OpenAI-compatible endpoint.

Clone the public repository and its pinned Hermes submodule:

```sh
git clone --recurse-submodules https://github.com/Displayer226/sillytavern-hermes-bridge.git sillytavern-hermes-bridge
cd sillytavern-hermes-bridge
git -C hermes-agent rev-parse HEAD
# Expected: 5661709c997cb5557cc337fd428b44c598ab43ca
```

## Configure credentials and provider

Create a private Compose environment file. The tracked example contains placeholders and local defaults; it is suitable for configuration validation, not startup.

```sh
cp .env.public.example .env.public
chmod 600 .env.public
openssl rand -hex 32  # generate WS_TOKEN
openssl rand -hex 32  # generate HERMES_DASHBOARD_BASIC_AUTH_SECRET
openssl rand -base64 24  # generate HERMES_DASHBOARD_BASIC_AUTH_PASSWORD
```

Set a private username and the three independently generated values in `.env.public`. Set `CORS_ORIGINS` to the exact browser origin used by SillyTavern, including scheme and port. Do not reuse the model-provider key for the proxy token or Dashboard credentials. Keep `.env.public` outside version control.

Run the preflight before any Compose command that starts a container. It reports missing, duplicate, or placeholder settings without printing their values.

```sh
python3 scripts/preflight-public-quickstart.py .env.public
```

Configure Hermes with an interactive terminal before starting the long-running services. For an account provider, open the setup wizard:

```sh
docker compose --env-file .env.public -f compose.public.yaml run --rm hermes setup
```

Select the provider and model, enter its key only when required, and follow the wizard's tool-selection prompts. Then persist the no-tools policy for the default profile:

```sh
docker compose --env-file .env.public -f compose.public.yaml run --rm hermes config set context.engine compressor
docker compose --env-file .env.public -f compose.public.yaml run --rm hermes config set agent.disabled_toolsets '[all]'
```

For a keyless OpenAI-compatible endpoint, configure its model id and URL separately. The URL must be reachable from the Hermes container; `localhost` inside that container refers to Hermes itself. Do not put credentials in the URL.

```sh
MODEL_ID=your-model-name
MODEL_BASE_URL=http://model-hostname:8080/v1
docker compose --env-file .env.public -f compose.public.yaml run --rm hermes config set model.provider custom
docker compose --env-file .env.public -f compose.public.yaml run --rm hermes config set model.default "$MODEL_ID"
docker compose --env-file .env.public -f compose.public.yaml run --rm hermes config set model.base_url "$MODEL_BASE_URL"
docker compose --env-file .env.public -f compose.public.yaml run --rm hermes config set model.api_mode chat_completions
```

Run each noninteractive `config set` command separately. If scripting them, redirect stdin from `/dev/null`. Apply the no-tools settings above after either provider setup path. Hermes stores provider configuration in the named `hermes-state` volume, not in this repository.

Validate the Compose file, then build and start the local stack:

```sh
docker compose --env-file .env.public -f compose.public.yaml config --quiet
docker compose --env-file .env.public -f compose.public.yaml build
docker compose --env-file .env.public -f compose.public.yaml pull proxy-ingress
docker compose --env-file .env.public -f compose.public.yaml up -d --wait hermes proxy proxy-ingress
```

The Compose configuration sets `HERMES_PROFILE_ALLOWLIST=default`, pins the default TUI to the `context_engine` toolset, and disables registered toolsets. The proxy enforces the profile restriction server-side, including API requests and restored session state. The authenticated schema probe creates a disposable default-profile session, waits for the completed agent build, requires `tools.show` to return zero schemas, and closes that session without submitting a prompt.

```sh
docker compose --env-file .env.public -f compose.public.yaml run --rm --no-deps -T \
  -v "$PWD/scripts:/checks:ro" proxy python /checks/check-public-tool-schema.py \
  </dev/null
curl -fsS http://127.0.0.1:8010/health/ready
```

## Install the extension

Build the extension and copy its manifest and generated bundle into SillyTavern's third-party extension directory:

```sh
ST_DIR=/path/to/SillyTavern
npm --prefix responses-proxy ci
npm --prefix responses-proxy run build
mkdir -p "$ST_DIR/public/scripts/extensions/third-party/responses-proxy"
cp responses-proxy/manifest.json "$ST_DIR/public/scripts/extensions/third-party/responses-proxy/"
cp -a responses-proxy/dist "$ST_DIR/public/scripts/extensions/third-party/responses-proxy/"
```

Reload SillyTavern and enable **Responses Proxy**. In API Connections, select **Custom OpenAI** and set the base URL to `http://127.0.0.1:8010/v1`, the API key to the `WS_TOKEN` from `.env.public`, and the model to `hermes`. Set the same proxy token in the extension panel. If needed, set the WebSocket override to `ws://127.0.0.1:8010/ws`. Leave profile, model, and workspace overrides empty.

After entering or changing the proxy token in the extension panel or the WebSocket URL override, reload SillyTavern again. The extension does not reconnect live when these settings change. After the reload, reconnect **Custom OpenAI** in API Connections if SillyTavern no longer shows it as connected.

## Smoke check and limits

1. Confirm that only the ingress is published and bound to loopback with `docker compose ... ps` and `docker compose ... port proxy-ingress 8080`.
2. Confirm `/health/ready` reports the Hermes WebSocket connected and ready, and the schema probe reports zero tools.
3. In a new SillyTavern chat, send a plain text message and confirm a streamed answer.
4. Restart only the proxy, reopen the same chat, and send another text turn.

The stack does not mount `/var/run/docker.sock`, a host workspace, or host networking. Hermes has a separate model-egress network; the proxy has no external route. Keep the published endpoint loopback-only. The proxy container currently runs as root; review that boundary before treating the stack as a beta.
