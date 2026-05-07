# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] — 2026-05-07 — "Audible, Streaming, DeepSeek-aware"

Ports three internal feature modules into the public tree (modular and
individually revertible), plus reliability fixes for shutdown and session
replay uncovered during integration.

### Added

- **Token-streaming TUI dashboard.** The right pane now renders LLM tokens,
  tool calls, and tool results in real time instead of a single batched
  reply. Earth-tones palette, rounded borders, self-rendered scrollback
  with PageUp/PageDown/End + mouse-wheel, `TOKEN_FLUSH_CHARS=80` /
  `TOKEN_FLUSH_INTERVAL_SECONDS=0.25` throttling, 0.4s farewell frame on
  Ctrl+C.
- **Startup sound on `syll wake`.** Packaged WAV plays once during the
  splash, cross-platform (macOS `afplay`, Linux
  `paplay`/`pw-play`/`aplay`/`ffplay`, Windows `winsound`), non-blocking.
  Configurable via `Config.startup.sound.{enabled, path}` —
  default-enabled, no migration needed for existing
  `~/.syll/config.json`.
- **DeepSeek thinking-mode support.** `LLMResponse.provider_extra` now
  carries `reasoning_content` end-to-end through the agent loop,
  subagent, streaming, and session JSONL, so multi-round tool calls no
  longer hit `400 reasoning_content must be passed back`.
- **`web.streaming.process_streaming`** accepts keyword-only `channel` /
  `chat_id`, so non-web callers (e.g. the CLI dashboard) can
  self-identify without mis-tagging messages as `web`.
- **Wheel build** now ships `syll/assets/**/*.wav`.

### Fixed

- `Session.get_history` drops orphan `tool` messages whose
  `tool_call_id` no longer matches a recent assistant `tool_calls`
  entry, defending against partially-replayed turns.
- `web.streaming.process_streaming` no longer persists half-built turns
  when the provider returns `Error calling LLM: ...`, so a transient
  400 cannot poison the next request through session JSONL.
- `chat_ws` catches `asyncio.CancelledError` alongside
  `WebSocketDisconnect`, so uvicorn-initiated cancellations drain
  silently.
- `wake()` waits up to 3 s for uvicorn to finish its own graceful
  shutdown before forcing task cancellation, eliminating the
  `starlette.routing ... asyncio.exceptions.CancelledError` traceback
  that used to appear at exit.
- `web/routes/sessions.py` strips `reasoning_content` and
  `provider_extra` before serving session history to the frontend
  (replay-only metadata, not user-visible content).

---

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
  `micasa99.github.io/syll/`.
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
