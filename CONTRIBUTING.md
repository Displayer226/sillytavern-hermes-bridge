# Contributing

Keep changes focused on the bridge, proxy, or extension. The public repository is a monorepo: make coordinated changes together and describe any required compatibility between the proxy, extension, and pinned Hermes commit.

Before submitting changes, run these local checks:

```sh
(cd proxy-ST && python -m pip install -r requirements-dev.txt && pytest -q)
(cd proxy-ST && ruff check proxy_st tests)
(cd proxy-ST && mypy --strict proxy_st/schemas.py proxy_st/health.py)
(cd responses-proxy && npm ci && npm run test:unit && npm run build)
docker compose --env-file .env.public.example -f compose.public.yaml config --quiet
```

The strict mypy check is intentionally scoped to `proxy_st/schemas.py` and `proxy_st/health.py`; the repository-wide mypy check is not a passing CI claim. Compose validation parses and interpolates configuration only; it does not start containers. CI also scans the full checkout with Gitleaks CLI 8.30.1, pinned and checksum-verified. Its failure summary reports only the file, line, and rule; it never prints the detected value. Do not include credentials, chat data, Hermes state, logs, databases, generated bundles, or local configuration.

The current Quick Start supports a local text-only default-profile path. Changes that enable tools, workspaces, voice, custom profiles, remote access, or additional users need a separate security review and tests before those features are documented as supported.

By submitting a contribution to the bridge-owned code, you agree to license that contribution under AGPL-3.0-only, the license of this repository. The `hermes-agent/` submodule is maintained separately under its own MIT license; propose changes to Hermes in that project's repository.
