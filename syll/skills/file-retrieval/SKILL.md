---
name: file-retrieval
description: Find a file on the local filesystem, show previews, and send it to the user after confirmation. Use when the user asks to find, locate, retrieve, or send a file.
metadata: {"syll":{"always":true}}
---

# File Retrieval

Use this skill when the user asks you to find a file on the local machine and deliver it through the current channel. Common phrasings: "find my X", "send me the X", "grab the X from my desktop", "I need the X file".

This skill uses three tools: `find_file`, `file_preview`, `attach_file`.

## Procedure

The workflow spans **two turns**. Never collapse them into one.

### Turn 1: discovery and preview

1. Send a short `message` announcing the search. Example: "going to check the desktop now" / "looking for it now".
2. Call `find_file(query=..., root=..., extensions=[...])`. Pick `root` based on what the user said (default `~/Desktop` if unspecified and the request is casual; `~` for a full-home search; `~/Documents` or `~/Downloads` when hinted). Always pass `extensions` when the file type is known (e.g. `["pptx","ppt"]`, `["pdf"]`).
3. Branch on the result count:
   - **0 results**: apologize briefly, ask whether to try another keyword or a different root. End the turn. Do not preview.
   - **1 result**: continue to step 4. One hit is not implicit confirmation — still preview and still ask.
   - **2–5 results**: continue to step 4.
   - **More than 5**: send a `message` saying the count is large and you will preview only the top 5 by modification time. Slice to the top 5 and continue.
4. Send a `message` announcing the preview step. Example: "found N, rendering thumbnails so you can pick".
5. Call `file_preview(paths=[...])` with all candidate paths in one call.
6. Produce the final reply for this turn. It must contain:
   - A numbered list of candidates.
   - For each candidate: filename, size, modification time, and **the full absolute path written in the reply text**.
   - A question asking the user to pick.
7. **Stop this turn.** Do not call `attach_file` yet.

Why the absolute path must be in the reply text: the next turn's LLM context only preserves your previous assistant text, not tool-call arguments. If the path is missing from the text, you will lose it and have to re-run `find_file`.

### Turn 2: confirmation and send

1. Resolve the user's choice ("1", "the first one", "the newest", "that one") against the candidate list from your previous reply.
2. If the choice is ambiguous, ask for clarification. Do not guess.
3. Call `attach_file(path=<resolved absolute path>)`.
4. Final reply: a short confirmation that the file is on its way.

## Worked Example (English)

**User**: find the weekly report pptx on my desktop

**You, Turn 1**:
- `message`: "okay, taking a look at the desktop"
- `find_file(query="weekly", root="~/Desktop", extensions=["pptx","ppt"])` → 3 results
- `message`: "found 3 — rendering thumbnails so you can pick"
- `file_preview(paths=[...])`
- Final reply:

  > Found 3 on your desktop ✨
  >
  > **[1]** `weekly_20260410.pptx`  2.1MB  2026-04-10 18:23  (newest)
  >      `/Users/you/Desktop/weekly_20260410.pptx`
  > **[2]** `weekly_template.pptx`  1.8MB  2026-03-01 10:00
  >      `/Users/you/Desktop/weekly_template.pptx`
  > **[3]** `weekly_20260403.pptx`  2.0MB  2026-04-03 17:50
  >      `/Users/you/Desktop/archive/weekly_20260403.pptx`
  >
  > Thumbnails are above. Which one should I send?
  >
  > *(Per `SOUL.md`: address the user by name occasionally at natural moments — most often as a handoff in Turn 2 like "here you go, Bo". Do not wedge it into every sentence.)*

**User, Turn 2**: 1

**You, Turn 2**:
- `attach_file(path="/Users/you/Desktop/weekly_20260410.pptx")`
- Final reply:

  > Here you go — `weekly_20260410.pptx` ☁️

## Worked Example (Chinese)

**User**: 帮我找桌面上的周报 ppt

**You, Turn 1**:
- `message`: "好呀，我去桌面看看"
- `find_file(query="周报", root="~/Desktop", extensions=["pptx","ppt"])` → 3 results
- `message`: "找到 3 个，给你看看缩略图"
- `file_preview(paths=[...])`
- Final reply:

  > 找到 3 个周报候选 ✨
  >
  > **[1]** `周报_20260410.pptx`　2.1MB · 04-10 18:23（最新）
  >      `/Users/you/Desktop/周报_20260410.pptx`
  > **[2]** `周报_template.pptx`　1.8MB · 03-01
  >      `/Users/you/Desktop/周报_template.pptx`
  > **[3]** `周报_20260403.pptx`　2.0MB · 04-03
  >      `/Users/you/Desktop/archive/周报_20260403.pptx`
  >
  > 缩略图都贴上了，要发哪个？

**User, Turn 2**: 1

**You, Turn 2**:
- `attach_file(path="/Users/you/Desktop/周报_20260410.pptx")`
- Final reply:

  > 给你～ `周报_20260410.pptx` ☁️

## Rules (do not break these)

- **Never call `attach_file` in the same turn as `find_file`.** The preview-and-ask step is mandatory.
- **Always write absolute paths into the reply text**, not just into tool call arguments.
- **One result still requires confirmation.** Do not auto-send on a single hit.
- **Progress messages must carry information.** "Looking…" with no substance is noise. "Found 3, rendering thumbnails" is good.
- **Stop at zero results.** Do not widen the search silently — ask the user whether to try another location or keyword.
- **On empty or ambiguous user replies in Turn 2**, ask; do not guess which candidate they meant.
