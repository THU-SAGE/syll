# ADR 0003: Safe Default Bind and Request Hardening

## Status

Accepted

## Context

A pre-release review surfaced several defaults and boundaries in the web/config
layer that were not safe-by-default for a self-hosted companion that a new user
runs with `syll wake`:

- `gateway.host` defaulted to `0.0.0.0`, so a fresh install listened on every
  interface. The admin guard only gates unsafe methods, so several `GET` data
  routes were reachable unauthenticated from the LAN, while the README implies a
  localhost-only experience.
- `web_fetch` validated only the URL scheme, so the agent could fetch
  loopback / link-local / private addresses, and it followed redirects without
  re-validating each hop (SSRF).
- The `restrict_to_workspace` file fence used a string-prefix check, which a
  sibling directory sharing the workspace name as a prefix could escape.
- `config.json` (API keys, bot tokens) was written with default permissions.
- `GET /api/v1/config` is an intentional public read, but returned `api_base`/
  `proxy` URL fields verbatim, disclosing any embedded `user:pass@` credentials.

Separately, the documented `SYLL__SECTION__FIELD` environment-variable form did
not resolve, because the settings prefix was `SYLL_` (single underscore) with a
`__` nested delimiter.

## Decision

- Default `gateway.host` to `127.0.0.1`. Binding beyond loopback is an explicit
  opt-in (config or CLI flag), keeping the documented localhost experience true
  by default.
- In `web_fetch`, resolve the host and reject any IP that is loopback, private,
  link-local, reserved, multicast, or unspecified (including IPv4-mapped IPv6),
  and re-validate every redirect hop.
- Replace the workspace path fence with real path containment.
- Write `config.json` `0600` via an atomic temp-file replace; set the parent
  directory `0700`.
- Strip `user:pass@` userinfo from URL-valued fields in the `GET /config`
  response (the endpoint stays a public read; only the credential leak is
  closed).
- Set the settings `env_prefix` to `SYLL__` so the documented
  `SYLL__SECTION__FIELD` form resolves.

## Consequences

A fresh install is loopback-only and matches the README; the SSRF surface in the
agent's fetch tool is closed; the workspace fence and at-rest config permissions
behave as intended; and environment-variable configuration works as documented.

Operators who relied on the previous `0.0.0.0` default must now opt in to bind on
all interfaces. The single-underscore `SYLL_SECTION__FIELD` form (previously the
only one that worked) is no longer special-cased; the documented double-
underscore form is canonical.

Revisit if remote/multi-user deployment becomes a first-class mode, which would
warrant authenticated GET data routes and a dedicated remote-admin posture
rather than a loopback-only default.
