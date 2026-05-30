---
name: audition-clean-voice
description: "Clean up a voice recording — remove hiss, hum, background noise, and harsh sibilance — by driving the real Adobe Audition app on a macOS host. Triggers: clean up audio, clean up the voice, remove hiss, remove hum, remove background noise, denoise, noise reduction, de-ess, reduce sibilance, repair voice, fix the recording. 中文触发词：降噪、去底噪、去杂音、去噪音、人声清理、清理录音、修复人声、去齿音、去咝声。"
metadata: {"syll":{"emoji":"🎚️","os":["darwin"],"requires":{"bins":["ffmpeg"]}}}
---

# Audition Clean Voice

Use the `clean_audio_in_audition` tool to clean up a voice recording — reducing
hiss, hum, broadband noise, and harsh sibilance. This is **not** a filter that
runs in this process — it drives the **real Adobe Audition application** on a
macOS host, opens the clip, applies the cleanup, and exports the result.

## When To Reach For This

The user wants a recording to sound cleaner. Recognize the intent from phrases
like:

- English: "clean up this audio", "clean up the voice", "remove the hiss",
  "remove the hum", "get rid of the background noise", "denoise this",
  "reduce the noise", "de-ess", "too much sibilance", "repair the voice",
  "fix the recording".
- 中文：「降噪」「去底噪」「去杂音」「去噪音」「帮我清理人声」「清理一下录音」「修复人声」
  「去齿音」「去咝声」。

## How It Works

1. You call `clean_audio_in_audition` with the path to the source audio file.
2. The tool launches / focuses Adobe Audition, opens the clip, applies the
   noise reduction / de-ess chain, and exports the cleaned audio.
3. The tool **measures** the result (noise-floor reduction, whether the voice
   was preserved vs. only made louder) and returns a verdict.
4. The before and after audio render inline automatically — you do not need to
   attach or describe them yourself.

## Confirm Before Control

This tool seizes the mouse and keyboard of the host machine. It MUST NOT take
over the screen without the user's explicit permission.

1. **First call — `confirmed=false`.** Always make the first call with
   `confirmed=false`. The tool will return a takeover-consent question (it does
   **not** touch the mouse/keyboard yet). Surface that question to the user.
2. **Wait for an explicit yes.** Only proceed once the user clearly agrees — a
   "yes", "go ahead", "do it", "确认", "可以" in the conversation counts. Silence,
   ambiguity, or "maybe" does **not** count.
3. **Second call — `confirmed=true`.** Only then call again with
   `confirmed=true`. This is the call that actually takes over the host.

Never set `confirmed=true` on the first call, and never assume consent.

## Honesty Protocol

Report **only** the verdict the tool returns. Do not embellish.

- The tool returns an outcome such as `excellent`, `pass`, `partial`,
  `gain_only`, or `failed`, plus a measured score (e.g. noise-floor reduction).
- If the verdict is `excellent` or `pass`, you may say the recording was
  cleaned / the noise was reduced.
- If the verdict is `partial`, say the cleanup is incomplete / needs review —
  do not claim it is clean.
- If the verdict is `gain_only`, the cleanup was **not proven**: the clip may
  only have been made louder, not actually denoised. Say so plainly — do not
  say "cleaned", "denoised", or "noise removed".
- If the verdict is `failed`, say it failed and offer to retry or try a
  different clip.

Never claim "cleaned", "denoised", or "noise removed" beyond what the measured
verdict supports. The user trusts the number, not your optimism.

## Usage Protocol

When the user asks to clean a recording:

1. **Confirm the source.** Make sure you have the path to the audio the user
   wants processed.
2. **First call, `confirmed=false`.** Relay the takeover-consent question.
3. **Get explicit consent.** Wait for the user's "go ahead".
4. **Second call, `confirmed=true`.** Run the real cleanup.
5. **Report the verdict honestly.** State the outcome exactly as measured. The
   before/after audio renders inline on its own.

## Example

User: "这段录音底噪好大，帮我降噪一下"

- First, call `clean_audio_in_audition(audio_path="/path/to/voice.wav",
  confirmed=false)`.
- The tool replies with a consent question; relay it: "This will take over the
  mouse and keyboard on the Mac to run Audition. Shall I go ahead?"
- User: "好的，开始吧"
- Then call `clean_audio_in_audition(audio_path="/path/to/voice.wav",
  confirmed=true)`.
- Tool returns verdict `gain_only`. You report honestly: "处理跑完了，但校验显示
  只是音量变大、降噪效果未被证实，因此我不能说底噪已去除。要不要换个参数或换一段再试？"
  The before/after audio appears inline.

## Do Not

- Do **not** drive `gui_action` directly to attempt the cleanup — use this
  dedicated tool, which knows the Audition workflow and measures the result.
- Do **not** call with `confirmed=true` before the user agrees.
- Do **not** overstate the result. A `gain_only` verdict is not a cleanup.
