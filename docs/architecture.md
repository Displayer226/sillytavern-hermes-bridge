# Architecture

The local Quick Start sends a SillyTavern chat through the Hermes Bridge extension and a loopback-only Nginx ingress to the session proxy. The session proxy forwards the conversation to the pinned Hermes Agent WebSocket API. Hermes reaches the selected model provider on a separate Docker network.

```text
SillyTavern + Hermes Bridge extension
             | HTTP / WebSocket on loopback
             v
      Nginx ingress
             | internal backend network
             v
      session proxy
             | internal backend network
             v
      Hermes Agent ---- model-egress network ---- model provider
```

The Quick Start uses named state volumes and the default Hermes profile. It publishes no Hermes or session proxy port, mounts no host workspace, and passes no Docker socket. The default-profile probe verifies that the newly built session exposes no tool schemas.

## Hermes dashboard authentication

The proxy supports password and OIDC authentication. OIDC authorization can
start from the extension's **Connecter Hermes** button; tokens remain on the
proxy and renew independently of the browser session. See
[Hermes browser sign-in](hermes-authentication.md) for the flow, operational
requirements and confirmed validation scope.
