# Hermes browser sign-in

The extension supports connecting the shared proxy to an OIDC-protected Hermes
dashboard directly from SillyTavern. Password authentication remains available.
See the [proxy authentication reference](../proxy-ST/README.md#sign-in-from-sillytavern)
for environment variables, routes and reverse-proxy configuration.

## User flow

When authorization is needed, reload SillyTavern and click **Connect Hermes**.
The browser opens the identity provider, reusing its existing SSO session when
possible. After any required consent, it returns to SillyTavern automatically.
No terminal command or callback URL copy is needed. The proxy reconnects immediately after the browser callback. CLI sign-in is
detected locally within five seconds, without contacting the identity provider.

Tokens stay in the proxy's private persistent storage. Ordinary page reloads
and service restarts do not require another browser authorization as long as
the saved renewable session remains valid. Revocation, refresh-token expiry,
removed credentials or changed OIDC configuration may require signing in again.
The CLI remains available as a recovery tool.

## Configuration to preserve

- Use the same OIDC issuer and client ID that Hermes verifies.
- Register the exact HTTPS callback `/hermes-auth/callback` on the SillyTavern
  origin. Allow the `authorization_code` and `refresh_token` grants and the
  `openid profile email offline_access` scopes. Keep the Hermes provider's
  requested scopes consistent with this renewable session.
- Preserve `WS_TOKEN` authentication for the proxy API and the extension.
  Authelia's `forward_auth` session alone does not supply Hermes credentials.
- Forward the callback to the proxy without stripping its path, retaining
  reverse-proxy authentication. Exclude callback codes and state from logs.
- Keep the credential file in a writable persistent directory. Rotated refresh
  tokens are saved by atomic replacement; mount its directory, not the file.
- The authorization belongs to the existing shared proxy connection; this does
  not add independent Hermes identities for each SillyTavern user.

## Validation record — 2026-10-15

The user confirmed successful browser sign-in and operation through SillyTavern
in the private deployment behind Authelia and Caddy. A subsequent server check
reported `websocket_connected=true`, `websocket_ready=true`, and HTTP 200 from
`/health/ready`.

Validation before that confirmation included 247 public-project proxy tests,
220 private-project proxy tests, Chromium tests of the extension's login UI,
production extension builds, configuration validation for Authelia and Caddy,
and authenticated checks of the public status and login-start routes.

The browser flow was validated against the private deployed Hermes fork. This
record does not establish an OIDC end-to-end run of the public Docker Quick
Start with its separately pinned Hermes revision. Refresh rotation and expiry
handling have automated coverage; a real provider expiry/rotation and a restart
after browser authorization have not been separately observed in this record.

## Troubleshooting

`401` for `/api/auth/ws-ticket`, followed by `404 Unknown provider` for
`/auth/password-login`, indicates that the proxy is trying password auth against
an OIDC-only Hermes configuration. Select `oidc` and use browser sign-in instead
of changing the password provider name to `self-hosted`.

If the button is absent, check the extension version, its proxy bearer token,
and `/v1/hermes/auth/status` with authentication. If sign-in fails, check the
registered callback, allowed scopes/grants, browser binding cookie and Hermes
identity verification. Restarting the proxy during authorization invalidates
the pending browser flow; start again from SillyTavern.

Use `/health/ready` to verify the full connection. `/health` reporting process
status `ok` alone does not establish Hermes readiness. Keep tokens, cookies,
callback URLs containing codes, and runtime configuration secrets out of Git.


## Expired sessions and provider outages

The proxy's password mode uses password authentication without an OIDC file.
OIDC mode requires a configured dashboard URL and token-file path and waits for operator login
when credentials are missing or rejected; it never falls back to password.
In auto mode an existing OIDC file takes priority, including a rejected session;
without one, configured password authentication is tried, followed by legacy
dashboard-token discovery on password failure. Without password credentials,
auto tries the legacy path. Legacy and disabled modes keep their existing rules.

The protected `/v1/hermes/auth/status` reports facts about the shared connection:
`connected` is the WebSocket state, `ready` additionally requires gateway
readiness, `login_required` is set after a confirmed need for OIDC login,
`session_expired` distinguishes a rejected saved session from a missing file,
and `retry_at` is the Unix timestamp for a scheduled retry, or null. During
startup before the first authentication attempt, none of these fields promises
that credentials have already been checked. A configured but empty OIDC token
path also requires an operator to correct the configuration before login works.
The banner reports a connected WebSocket with an unready gateway separately.
If a status request fails or returns invalid data, it reports status as
unavailable without inferring OIDC expiry. When disconnected with no login
requirement and no scheduled retry, the backend does not distinguish initial
connection from another transition; the banner therefore shows a neutral wait.

A definitive refresh-token rejection suspends automatic authentication requests and
persists a private `login_required` marker with the saved session. Restarting the
proxy does not retry that rejected token. Browser or CLI sign-in atomically
replaces the session and clears the marker. The extension displays **Hermes
session expired** and **Reconnect Hermes**; the browser authorization requires
the operator's interaction.

The pinned Hermes native refresh route returns 401 for a rejected refresh token.
Other refresh errors and a ticket rejection after a successful refresh do not
by themselves prove that the refresh token is invalid; credentials are kept and
the connection is retried. A 400 or 403 can require configuration repair rather
than a new login. Temporary provider failures retain the renewable session. Hermes forwards the
provider's `Retry-After` through its native refresh endpoint; the proxy respects
that minimum delay, including HTTP dates. Without a provider delay, reconnection
uses exponential backoff from one second to five minutes; a provider's longer
`Retry-After` is still honored. New browser credentials
interrupt the wait. The extension displays the next retry time while waiting.

Refresh and browser login writes are serialized within one proxy process. The
separate CLI and other external credential writers are not covered by that
in-process lock, so concurrent external writes and refresh rotation still require
operational coordination.

Keep Authelia's endpoint rate limits enabled. Increasing token lifetimes is an
optional operator policy, not a repair for already expired credentials. Never
remove production session storage to recover this condition.
