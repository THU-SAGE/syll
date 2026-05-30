# ADR 0002: Adobe Photoshop/Audition as Conversational Tools

## Status

Accepted

## Context

Driving creative desktop apps (Photoshop background cutout, Audition voice
cleanup) was previously prototyped as standalone web panels that ran their own
orchestration outside the agent loop. That shape does not fit Syll's model: a
capability should be something the agent reaches for in a normal conversation,
not a separate surface.

These tasks have three properties that shape the design:

- **They drive a real third-party desktop app** via the existing GUI-automation
  stack (`gui_action` / `gui_action_planned`), which seizes the user's mouse and
  keyboard for the creative step.
- **The result must be proven, not assumed.** The GUI agent's claim of success
  is not trusted; a deterministic verifier re-measures the exported artifact
  (alpha-channel coverage for cutouts, before/after audio metrics with an
  anti-gain-only check for cleanup).
- **They are platform- and app-bound** (macOS + the installed Adobe app +
  Accessibility permission), so they must fail with a plain-language explanation
  rather than a stack trace when the host cannot run them.

## Decision

Add the capability as dedicated agent tools (`photoshop_cutout`,
`clean_audio_in_audition`) plus an always-on `audio_inspect` analysis tool,
registered from `AgentLoop._register_default_tools()` (and the config hot-reload
path) only inside the existing `tools.gui.enabled` block, via a shared
`register_adobe_tools()` helper so the two registration sites cannot drift.

Framework-free cores live in a new `syll/agent/adobe/` subpackage (app bridges,
preflight, single-flight lock, progress bridge) plus `syll/audio/` (the
deterministic metrics engine). The tools enforce four contracts:

1. **Confirm before control** — a call with `confirmed=false` returns a consent
   prompt and never seizes input; only `confirmed=true` drives the GUI.
2. **Single-flight** — one shared, event-loop-safe lock; only one Adobe run
   touches the screen at a time.
3. **Honesty** — the returned verdict comes from the deterministic verifier; a
   `gain_only` / `REVIEW` result is reported as not-proven.
4. **Preflight first** — plain-language blockers (non-macOS, app missing,
   `osascript`/`ffmpeg` missing, Accessibility not authorized,
   monitor/skill-resolution mismatch) are returned before any side effect.

Markdown `SKILL.md` files advertise the capability for discovery. The optional
`Pillow` / `pyloudnorm` dependencies live under a `[adobe]` extra.

## Consequences

Photoshop/Audition become chat-native: the user attaches a file and asks in
natural language; before/after media renders inline through the existing
tool-result path with no new web panel. The GUI-automation spine is reused
rather than duplicated, and the deterministic verifiers keep the assistant from
overstating results.

The tools are macOS + Adobe specific and are invisible on hosts without GUI
automation enabled. Live progress streams best-effort over `broadcast_ws`; the
final result and verdict are identical on both the streaming and REST paths.

Revisit if a cross-platform or headless creative backend is added, or if the
single-flight model needs to become cooperative cancellation rather than a
TTL-reclaimed lock.
