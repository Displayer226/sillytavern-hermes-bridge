# Session proxy

This FastAPI service bridges SillyTavern-compatible chat completions and WebSocket events to a pinned Hermes Agent session. It keeps the SillyTavern chat id as the stable proxy session id and reports session readiness and tool events to the extension.

The repository Quick Start configures a local text-only path with the default Hermes profile. It does not enable workspaces, tool execution, voice, custom profile selection, or remote access.

## Development

```sh
python -m pip install -r requirements-dev.txt
python -m pytest
ruff check .
```

The Docker build uses only `Dockerfile`, `main.py`, `requirements.txt`, and Python files in `proxy_st/`. The `.dockerignore` keeps all other files out of its build context.

## License

AGPL-3.0-only. See the repository [LICENSE](../LICENSE).

## Hermes dashboard authentication

`HERMES_DASHBOARD_AUTH_MODE=password` keeps the existing username/password login.
Configure `HERMES_DASHBOARD_AUTH_PROVIDER` (default `basic`),
`HERMES_DASHBOARD_AUTH_USERNAME`, and `HERMES_DASHBOARD_AUTH_PASSWORD`.

For an OIDC dashboard, set:

```dotenv
HERMES_DASHBOARD_AUTH_MODE=oidc
HERMES_DASHBOARD_OIDC_TOKEN_FILE=/app/data/hermes-oidc.json
```

Run the one-time login inside the proxy container:

```sh
docker exec -it sillytavern-session-proxy python -m proxy_st.hermes_oidc \
  --public-url https://hermes.example.com \
  --manual
```

This is an advanced deployment: the public local Compose stack fixes the proxy
to password/basic and does not configure OIDC. It requires a separately designed
deployment and network configuration; setting OIDC variables in `.env.public`
or running this command in the unmodified local stack will not enable OIDC.
The command prints a URL to open in your browser. Sign in with your usual OIDC
provider, then paste the **full final loopback callback URL** into the command's
terminal prompt. A browser connection error on that last page is expected when
using `--manual`: the browser and container have separate loopback interfaces.
Do not paste the callback URL or token file into chat, issues, or logs.

Without `--manual`, the command opens a browser and receives the callback on a
local loopback listener. Use that mode when running the command on the same
machine as the browser, with `--token-file` pointing at runtime storage mounted
into the proxy. Login times out after five minutes.

`--public-url` is the externally accessible Hermes dashboard URL, including any
path prefix. Authorization and code exchange must reach the same dashboard
process because pending authorization codes are held in memory. If an outer
reverse-proxy login protects that URL, pass `--exchange-url` with the internal
URL of that **same dashboard process** so the command can exchange its PKCE code
without browser cookies (for example `http://host.docker.internal:9120/`). The dashboard
may be separate from `hermes serve` if both use the same OIDC provider/client
configuration. `--dashboard-url` defaults to `HERMES_DASHBOARD_URL` and records
the internal runtime URL in the credential file; pass it explicitly when logging
in outside the container. `--provider` defaults to `self-hosted`.

The credential file is written atomically with mode `0600`. Keep its containing
directory persistent and writable so refresh-token rotation can replace it;
mount the directory rather than a single file. Never put credentials in the
repository or image. Use one proxy process per credential file. Existing session
volumes at `/app/data` provide the required persistence.

The proxy sends the OIDC access token to `/api/auth/ws-ticket` and uses the
returned single-use ticket for its WebSocket. It refreshes tokens through
`/auth/native/refresh` before expiry or after an access-token rejection, saving
rotated refresh tokens before retrying. Network errors and provider outages keep
the saved credentials. An expired/revoked refresh token requires signing in again from SillyTavern
(or using the login command as a recovery tool); the proxy reloads the file on
each reconnect attempt.

`auto` prefers an existing OIDC file over configured password credentials.
An invalid saved OIDC session requires reauthentication rather than silently
switching identities. Without a saved file it retains the password/legacy
behavior. `legacy` supports older Hermes HTML tokens; `none`/`off` disable the
proxy's authentication handshake and do not bypass Hermes authentication.

After login, `/health` should report `websocket_connected=true` and
`websocket_ready=true`; `/health/ready` should return HTTP 200. The process-only
`/health` status can remain `ok` while Hermes is disconnected.

### Sign in from SillyTavern

The extension displays **Connect Hermes** when browser sign-in is configured
and the shared Hermes connection requires authentication. It opens the OIDC
provider in the current tab and returns to SillyTavern automatically. Tokens
stay on the proxy; no terminal command or token copy is needed.

Configure these additional proxy environment variables:

```dotenv
HERMES_DASHBOARD_AUTH_MODE=oidc
HERMES_DASHBOARD_OIDC_ISSUER=https://login.example.com
HERMES_DASHBOARD_OIDC_CLIENT_ID=hermes-dashboard
HERMES_DASHBOARD_OIDC_CALLBACK_URL=https://sillytavern.example.com/hermes-auth/callback
HERMES_DASHBOARD_OIDC_SCOPES=openid profile email offline_access
```

The client must be the one Hermes verifies (same issuer and client ID). Register
the exact callback URL with the provider, allow `authorization_code` and
`refresh_token` grants, and allow `offline_access`. The default client auth
method is `none` (public client with S256 PKCE). Confidential clients can set
`HERMES_DASHBOARD_OIDC_TOKEN_AUTH_METHOD=client_secret_basic` or
`client_secret_post` plus `HERMES_DASHBOARD_OIDC_CLIENT_SECRET` in private runtime
configuration. `HERMES_DASHBOARD_OIDC_PROVIDER` defaults to `self-hosted`.

A nonempty `WS_TOKEN` is mandatory when browser sign-in is configured. The
existing proxy bearer authentication protects `/v1/hermes/auth/status` and
`POST /v1/hermes/auth/login`. The extension must reach these routes on the same
public origin as the callback so its HttpOnly, Secure, SameSite=Lax binding
cookie can reach `/hermes-auth/callback`. The callback alone is exempt from the
proxy bearer check because browser navigation cannot attach that header. It
requires a single-use state and browser cookie, S256 PKCE and a matching OIDC
nonce. Hermes verifies the identity token before it is saved. Expired browser
flows require starting again after a proxy restart.

Forward `/hermes-auth/callback` to the proxy without stripping its path. Keep
this route behind your reverse-proxy authentication, and omit its query string
from access logs (it contains a one-time code). For Caddy, inside the SillyTavern
site block, alongside the existing authenticated `/proxy-ws/*` forwarding:

```caddyfile
@hermes_callback path /hermes-auth/callback
log_skip @hermes_callback
handle @hermes_callback {
    reverse_proxy sillytavern-session-proxy:8000
}
```

Preserve the site's existing Authelia `forward_auth` for this callback. A forward
auth session by itself does not mint a Hermes token; OIDC consent may still be
required. This authorizes the existing shared proxy connection and does not
introduce separate Hermes connections for each SillyTavern user.

For the confirmed deployment behavior and troubleshooting, see the
[Hermes browser sign-in record](../docs/hermes-authentication.md).
