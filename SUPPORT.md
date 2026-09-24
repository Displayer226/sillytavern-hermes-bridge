# Support policy

Support covers the current repository snapshot and the configuration documented in the [Public Docker Quick Start](docs/public-docker-quickstart.md). There are no versioned releases, long-term support branches, response-time guarantees, or hosted support service at this time.

## Supported setup

- The local, single-user, text-only SillyTavern-to-Hermes path described in the Quick Start.
- The pinned Hermes Agent revision recorded in `docs/export-provenance.md`.
- Only the Hermes `default` profile. The proxy rejects other profile selections, including API requests and restored session state.
- Hermes tools disabled, loopback-only ingress, and no host workspace or Docker socket mount.
- Linux with Docker Engine and Docker Compose v2.33.1 or newer, as listed by the Quick Start.

## Outside support

Voice, enabled tools, workspace access, non-default Hermes profiles, remote ingress, multi-user operation, other deployment topologies, and configurations that depart from the Quick Start are not supported. Reports for these setups may still be useful, but maintainers may ask for a reproduction using the supported configuration before investigating.

For ordinary bugs, use the issue form and include only redacted logs. For suspected vulnerabilities, follow [Security](SECURITY.md) and do not open a public issue.
