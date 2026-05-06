# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
