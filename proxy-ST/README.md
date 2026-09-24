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
