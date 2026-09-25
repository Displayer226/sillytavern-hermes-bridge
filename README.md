# SillyTavern Hermes Bridge

Use SillyTavern as the conversation frontend for a persistent Hermes Agent session. This repository is a technical preview with a local Docker Compose setup, a companion SillyTavern extension, and a session-aware proxy. Bugs and rough edges are expected; this is not a versioned release or demo announcement.

The supported path in this Quick Start is **text-only** and **Hermes Agent only**. It uses the default Hermes profile with tools disabled, publishes the ingress on loopback, and does not mount a host workspace or Docker socket. Voice, tools, custom profiles, remote access, and multi-user deployments are outside this setup.

Start with the [Public Docker Quick Start](docs/public-docker-quickstart.md). See [Architecture](docs/architecture.md) for the local request path.

See [Component provenance](docs/export-provenance.md) for the source revisions and export-specific adaptations.

See [Support](SUPPORT.md) for the supported setup and [Security](SECURITY.md) for vulnerability reporting and token handling.

This is a self-hosted integration, not a hosted service. Do not expose the proxy or Hermes dashboard directly to the Internet.

## License

The bridge-owned code and documentation in this repository, including `proxy-ST/` and `responses-proxy/`, are licensed under **AGPL-3.0-only**; see [LICENSE](LICENSE). The pinned `hermes-agent/` submodule is a separate project under its own MIT license. Third-party dependencies retain their respective licenses.
