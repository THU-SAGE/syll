---
name: photoshop-cutout
description: "Remove the background from an image and produce a transparent PNG by driving the real Adobe Photoshop app on a macOS host. Triggers: remove background, cut out, knock out the subject, isolate the subject, make a transparent PNG, transparent background, delete background. 中文触发词：抠图、扣图、去背景、去掉背景、扣出主体、透明背景、做透明 PNG。"
metadata: {"syll":{"emoji":"✂️","os":["darwin"],"requires":{"bins":["osascript"]}}}
---

# Photoshop Cutout

Use the `photoshop_cutout` tool to remove the background from an image and
deliver a transparent PNG. This is **not** a filter that runs in this process —
it drives the **real Adobe Photoshop application** on a macOS host, opens the
image, runs the cutout, and exports the result.

## When To Reach For This

The user wants a subject lifted off its background. Recognize the intent from
phrases like:

- English: "remove the background", "cut out the person/product/logo", "knock
  out the subject", "isolate this", "make it a transparent PNG", "give me a PNG
  with no background".
- 中文：「抠图」「扣图」「帮我去背景」「去掉背景」「把主体扣出来」「要透明背景」「做成透明 PNG」。

## How It Works

1. You call `photoshop_cutout` with the path to the source image.
2. The tool launches / focuses Adobe Photoshop, opens the image, performs the
   subject-selection cutout, and exports a transparent PNG.
3. The tool **measures** the result (how much background was actually removed,
   whether a clean alpha channel was produced) and returns a verdict.
4. The before image and the after PNG render inline automatically — you do not
   need to attach or describe them yourself.

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

Report **only** what the tool's measured verdict supports. Do not embellish.

The tool returns a Markdown report whose first line is either
`**Verdict**: PASS …` or `**Verdict**: REVIEW …`, followed by measured numbers
(transparent %, subject %, whether an alpha channel is present).

- **PASS** — the alpha checks passed: you may say the background was removed and
  a transparent PNG was produced.
- **REVIEW** — the file was exported but not all transparency checks passed.
  Say the cutout needs review / is not fully clean — do **not** claim the
  background was removed. Offer to retry or refine.
- **Export failed** (the verdict text says the cutout PNG was not created, shown
  as REVIEW) — say it failed and offer to retry or try a different image.

Never claim "done", "cut out", or "transparent" beyond what the verdict and the
measured ratios support. The user trusts the numbers, not your optimism.

## Usage Protocol

When the user asks for a cutout:

1. **Confirm the source.** Make sure you have the path to the image the user
   wants processed.
2. **First call, `confirmed=false`.** Relay the takeover-consent question.
3. **Get explicit consent.** Wait for the user's "go ahead".
4. **Second call, `confirmed=true`.** Run the real cutout.
5. **Report the verdict honestly.** State the outcome exactly as measured. The
   before/after images render inline on their own.

## Example

User: "帮我把这张产品图抠图，要透明背景"

- First, call `photoshop_cutout(image_path="/path/to/product.jpg",
  confirmed=false)`.
- The tool replies with a consent question; relay it: "This will take over the
  mouse and keyboard on the Mac to run Photoshop. Shall I go ahead?"
- User: "可以，去吧"
- Then call `photoshop_cutout(image_path="/path/to/product.jpg",
  confirmed=true)`.
- Tool returns `**Verdict**: PASS`. You report: "背景已去除，导出了透明 PNG（结果已通过校验）。"
  The before/after images appear inline. If it returns REVIEW instead, say the
  cutout needs another look rather than claiming success.

## Do Not

- Do **not** drive `gui_action` directly to attempt the cutout — use this
  dedicated tool, which knows the Photoshop workflow and measures the result.
- Do **not** call with `confirmed=true` before the user agrees.
- Do **not** overstate the result. A `REVIEW` verdict is not a clean cutout.
