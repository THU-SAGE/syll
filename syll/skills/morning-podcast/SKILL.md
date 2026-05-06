---
name: morning-podcast
description: Generate a short spoken morning news briefing as an audio clip.
metadata: {"syll":{"emoji":"🎙","always":false}}
---

# Morning Podcast

Triggered either by the user saying "morning podcast" / "晨间播客" or
by a cron job that runs this skill on a schedule. The output is a
single audio file (via the `speak` tool) that the web frontend will
auto-render as an `<audio>` element; if the job is delivering to a
messaging channel, the MP3 travels along the outbound message.

## Steps

1. **Collect stories.** Use `web_search` to gather 3–5 items across
   tech, world, and markets. One or two searches is usually enough:
   ```
   web_search(query="今日科技头条", count=5)
   web_search(query="today world news", count=5)
   ```
   Skim titles/snippets, pick the most substantive stories, and drop
   anything paywalled or clickbait-y.

2. **Summarize in spoken form.** For each story, write ~80–120
   Chinese characters (or ~40 English words). No markdown. No URLs in
   the spoken body — reference the outlet by name instead. Rewrite
   headlines into natural narration ("今天…", "另一边…").

3. **Compose one script.** Assemble a single continuous string:
   - Greeting using `{{ghost_name}}`, e.g. "{{ghost_name}} 早安播
     报，今天是 X 月 Y 日。"
   - Item 1 → item N with short transitions.
   - Sign-off: "以上就是今天的晨间播报，{{user_name}}，祝你有个顺
     利的一天。"

4. **Call `speak` exactly once** with the full script:
   ```
   speak(text="<full assembled script>")
   ```
   A single call yields the best prosody. Do **not** call `speak`
   per-item — concatenating multiple clips sounds choppy.

5. **Reply briefly.** One sentence summarizing what's in the briefing
   (e.g. "今日播报已生成：科技 / 市场 / 国际 共 4 条"). The audio
   itself is attached through the tool result's `media` — you don't
   need to repeat the script in the reply.

## Notes

- Real tool name is `web_search` (not `web.search`) and `speak`.
- If `speak` fails, fall back to a text-only briefing and tell the
  user why ("TTS unavailable: …").
- Never include URLs in the TTS input — Volcengine reads them out
  character-by-character and it sounds terrible.
