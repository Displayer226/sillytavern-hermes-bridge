# Security policy

## Supported security scope

The supported configuration is the local, text-only Quick Start: SillyTavern connects to the pinned Hermes Agent through this proxy; only the `default` Hermes profile is allowed; Hermes tools are disabled; the published ingress is bound to loopback; and no host workspace or Docker socket is mounted. The proxy enforces the profile restriction for API requests and restored sessions as well as ordinary requests.

Voice, enabled tools, workspaces, non-default profiles, remote ingress, and multi-user deployments are outside the supported security scope. Disabling tools and allowing only `default` narrows the available actions; it does not make prompts or provider traffic risk-free. Treat submitted text and stored session data as sensitive.

## Reporting a vulnerability

Do not report security vulnerabilities in a public issue. GitHub private vulnerability reporting is enabled for this repository; use its **Report a vulnerability** link. No separate maintainer email address is published.

Include the affected component and version, impact, reproduction steps, and any mitigation you have found. Do not include working credentials, provider keys, proxy tokens, private prompts, chat transcripts, session databases, or unredacted logs. Give maintainers reasonable time to investigate and coordinate a fix before public disclosure.

## Proxy token and deployment risks

`WS_TOKEN` is a bearer credential for proxy clients. Anyone who obtains it may authenticate to the proxy's permitted HTTP or WebSocket interface, submit prompts to the default Hermes session, and trigger model-provider usage and charges. Generate a long random value, keep it separate from provider and Dashboard credentials, and store it only in the private `.env.public` file and the local SillyTavern configuration. Do not put it in source control, URLs, screenshots, issue reports, or logs. Rotate it promptly if it may have been exposed.

Keep the ingress bound to loopback and do not expose the proxy or Hermes Dashboard directly to the Internet. CORS settings constrain browser origins; they do not replace token authentication or network isolation. A local process that can reach the loopback port and obtain the token can use the proxy.

The Quick Start's `LOG_INCLUDE_BODIES=false` setting avoids logging request bodies, but it is not a reason to publish logs without review. Redact credentials, host and user identifiers, local paths, session identifiers, and conversation content before sharing diagnostic output.
