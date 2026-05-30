# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Photoshop and Audition conversational tools (`photoshop_cutout`,
  `clean_audio_in_audition`, plus an always-on `audio_inspect`): drive the real
  Adobe app on a macOS host and return a measured before/after verdict. Gated
  behind `tools.gui.enabled`; optional deps under the `[adobe]` extra. See
  ADR-0002.
- `privacy.event_log_mode` (`full` | `summary` | `off`) to control how much
  conversation text is persisted to the event log.

### Changed

- Default `gateway.host` is now `127.0.0.1` (loopback). Binding to `0.0.0.0` is
  an explicit opt-in. See ADR-0003.

### Fixed

- The documented `SYLL__SECTION__FIELD` environment-variable form now resolves
  to the matching config field.
- Chat and intent inputs no longer send while an IME composition is active
  (e.g. pressing Enter to commit a Chinese/Japanese candidate).
- `REST /chat/message` now persists the full tool-call turn to the session,
  matching the WebSocket path.

### Removed

### Security

- `web_fetch` now rejects loopback / private / link-local / reserved addresses
  and re-validates each redirect hop (SSRF).
- `config.json` is written `0600` via an atomic replace; its parent directory is
  set `0700`.
- `GET /api/v1/config` strips `user:pass@` credentials from URL-valued fields.
- `restrict_to_workspace` uses real path containment (no sibling-prefix escape).

## [0.2.0] — 2026-04-14 — "First Public Syll Release"

This release is the first consolidated public Syll release. Packaging, CLI
commands, workspace layout, docs, and site all land under one name.

### Changed

- **Package layout**: the top-level Python package directory is now `syll/`.
- **CLI surface**: use `syll wake`, `syll web`, `syll onboard`,
  `syll agent`, `syll status`.
- **Config and workspace directory**: `~/.syll/`.
- **Environment variable prefix**: `SYLL__`.
- **Skill metadata JSON key**: frontmatter uses `metadata: {"syll": {...}}`.
- **GitHub repo**: canonical home is now `THU-SAGE/syll`. Project page:
  `thu-sage.github.io/syll/`.
- **WhatsApp bridge**: auth session directory is now `~/.syll/whatsapp-auth/`.

### Added

- `docs/references/channels.md` — full channel setup reference (fills a
  previously-broken README link).
- `docs/references/local-models.md` — local OpenAI-compatible model setup
  reference for vLLM, Ollama, LM Studio, llama.cpp (fills a previously-broken
  README link).
- `CHANGELOG.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`.
- GitHub Actions CI workflow running `ruff` + `pytest` on pull requests.

### Removed

- Personal development notes and adjacent research projects that were
  previously tracked in the repo but unrelated to the shipped product.

### Quick install

```bash
pip install syll
syll wake
```

---

## [0.1.x] — historical

Earlier pre-0.2 releases are preserved in the git history.
