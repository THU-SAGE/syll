<div align="center">
  <img src="docs/0_1.jpeg" alt="a small ghost holding a dark rectangle, looking downward" width="260">

  <h1>Syll</h1>

  <p>
    <em>a small, self-hosted AI companion<br/>
    who sits at the edge of your screen and quietly tends<br/>
    the things you almost forgot</em>
  </p>

  <p>
    <img src="https://img.shields.io/badge/version-0.2.0-blue" alt="Version">
    <img src="https://img.shields.io/badge/status-alpha-orange" alt="Status: alpha">
    <img src="https://img.shields.io/badge/python-≥3.11-blue" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </p>

  <p>
    <strong>Syll is a self-hosted companion runtime with a web UI, chat channels, proactive rituals, editable markdown skills, recorded workflows, and a desktop ghost.</strong>
  </p>

  <p>
    <a href="https://thu-sage.github.io/syll/"><strong>Project page →</strong></a>
    &nbsp;·&nbsp;
    <a href="#quick-start">Quick start</a>
    &nbsp;·&nbsp;
    <a href="#demo-workbench">Demos</a>
    &nbsp;·&nbsp;
    <a href="docs/lore.md">The Full Story · 故事</a>
    &nbsp;·&nbsp;
    <a href="https://thu-sage.github.io/syll/research.html">Research</a>
    &nbsp;·&nbsp;
    <a href="docs/report/syll-report-v1.pdf">Paper</a>
  </p>
</div>

---

## Feature news

- **MCP servers are now first-class tools.** Configure stdio, SSE, and streamable-HTTP MCP servers from the Pet UI; Syll loads them into the agent as namespaced `mcp__server__tool` tools before the first message.
- **Safer MCP activation flow.** Local stdio servers require a command preview + hash confirmation before anything launches, while env/header secrets stay masked in MCP responses and out of `/api/v1/config`.
- **Creative MCP templates included.** The public build ships Playwright, Stagehand, Blender, and Godot template suggestions; Blender uses `uvx blender-mcp`, and Godot pins GoPeak MCP for reproducible local game-scene automation.
- **Static asset refresh polish.** The web app now cache-busts bundled JS/CSS assets so UI changes reach the browser without stale `app.js` surprises after the next restart.

---


## Quick start

Syll works with any Python 3.11+ environment. Pick either main path, then run the shared install steps.

### Option A — conda

```bash
conda create -n syll python=3.11 -y
conda activate syll
```

### Option B — venv

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
```

### Install and wake

```bash
python -m pip install -U pip
python -m pip install syll

syll onboard
syll wake
```

Open `http://localhost:18790`, click the **Pet** tab, give your ghost a name (or keep *Syll*), and start a conversation.

> [!NOTE]
> The package, CLI, and import path are all `syll`. **Syll is the default persona** inside the companion you just installed; rename her to anything via the Pet tab at any time without touching code.

<details>
<summary><b>uv fast path</b></summary>

```bash
uv venv
source .venv/bin/activate
uv pip install syll
syll onboard
syll wake
```

</details>

### Configure a model

Set your LLM credentials in `~/.syll/config.json`, or finish setup from the web UI after `syll wake`:

```json
{
  "models": {
    "chat": {
      "model": "openrouter/openai/gpt-4o-mini",
      "api_key": "YOUR_API_KEY"
    }
  }
}
```

Get keys: [OpenRouter](https://openrouter.ai/keys) · [OpenAI](https://platform.openai.com) · [Anthropic](https://console.anthropic.com). Local models via vLLM / Ollama are also supported — see [docs/references/local-models.md](docs/references/local-models.md).

### Troubleshooting

- **`syll onboard` says "Config already exists"** — answer `N` unless you intentionally want to replace your current `~/.syll/config.json` and workspace.
- **`syll` runs from the wrong environment** — prefer `python -m pip install syll`, then check `which python`, `python -m pip -V`, and `which syll` point at the same environment.
- **`SYLL__*` env vars are not picked up** — use the `SYLL__SECTION__FIELD=value` shape, export the variable in your shell, and restart Syll.


---

## Visual tour

Click any card to watch the short MP4.

<table>
  <tr>
    <td width="50%" align="center">
      <a href="docs/media/tour/01-config-setup.mp4"><img src="docs/media/tour/01-config-setup.jpg" alt="Configuration setup" width="100%"></a><br>
      <b>1. Configuration</b><br>
      <sub>Set models, channels, identity, and runtime options from the browser.</sub>
    </td>
    <td width="50%" align="center">
      <a href="docs/media/tour/02-gui-click-calibration.mp4"><img src="docs/media/tour/02-gui-click-calibration.jpg" alt="GUI click calibration" width="100%"></a><br>
      <b>2. GUI calibration</b><br>
      <sub>Inspect screenshots, calibrate coordinates, and make desktop actions reproducible.</sub>
    </td>
  </tr>
  <tr>
    <td width="50%" align="center">
      <a href="docs/media/tour/03-skill-editor.mp4"><img src="docs/media/tour/03-skill-editor.jpg" alt="Skill editor" width="100%"></a><br>
      <b>3. Skill editor</b><br>
      <sub>Edit markdown skills in the web UI and publish reusable procedures.</sub>
    </td>
    <td width="50%" align="center">
      <a href="docs/media/tour/04-leading-mode.mp4"><img src="docs/media/tour/04-leading-mode.jpg" alt="Syll leading mode" width="100%"></a><br>
      <b>4. Leading mode</b><br>
      <sub>Let Syll guide an interactive task while the desktop ghost stays present.</sub>
    </td>
  </tr>
  <tr>
    <td width="50%" align="center">
      <a href="docs/media/tour/05-schedule-setup.mp4"><img src="docs/media/tour/05-schedule-setup.jpg" alt="Scheduled task setup" width="100%"></a><br>
      <b>5. Scheduling</b><br>
      <sub>Install rituals or schedule your own recurring jobs and workflow replays.</sub>
    </td>
    <td width="50%" align="center">
      <a href="docs/media/tour/06-web-chat.mp4"><img src="docs/media/tour/06-web-chat.jpg" alt="Web chat interface" width="100%"></a><br>
      <b>6. Web chat</b><br>
      <sub>Chat locally from the browser while watching runtime state and logs.</sub>
    </td>
  </tr>
  <tr>
    <td width="50%" align="center">
      <a href="docs/media/tour/07-persona-memory.mp4"><img src="docs/media/tour/07-persona-memory.jpg" alt="Persona and memory workspace" width="100%"></a><br>
      <b>7. Persona & memory</b><br>
      <sub>Shape the companion persona and maintain layered memory notes.</sub>
    </td>
    <td width="50%" align="center">
      <a href="docs/media/tour/08-demo-recording.mp4"><img src="docs/media/tour/08-demo-recording.jpg" alt="User demonstration recording" width="100%"></a><br>
      <b>8. Demo recording</b><br>
      <sub>Capture a live desktop routine into video and editable step traces.</sub>
    </td>
  </tr>
</table>

---

## What Syll can do

**She finds files you can't find.**
Ask her from your phone to grab the weekly report from your desktop. She walks your filesystem, renders thumbnails, shows you the candidates, and hands you the one you pick.

**She keeps soft watch over unfinished things.**
The draft you open and close without writing anything. The photo folder from three years ago. She notices. She doesn't nag.

**She reaches out sometimes.**
Morning light, evening winding down, a Tuesday afternoon when a memory drifts back. Not every time. Only when it would feel real. You control the cadence.

**She can live on your phone instead of your desk.**
Pair her with Feishu, Telegram, Discord, or WhatsApp and she'll come with you.

**She teaches herself new tricks.**
Give her a markdown skill (`SKILL.md`) and she loads it when the moment calls for it. Capture a GUI workflow once in the Demo tab, clean it up, and she can replay or schedule it later.

**She comes with a visual workbench, not just a terminal.**
Models, channels, schedule rules, identity, skills, and recorded workflows all have a browser surface now — so ordinary users can configure her without living in a CLI.

**She remembers in layers.**
The Memory tab surfaces long-term notes, today's journal, daily fragments, and an activity heatmap so her recollection feels like a notebook instead of a raw log dump.

**She can stay at the edge of your desktop.**
The optional desktop ghost mirrors her state, reacts to wake/sleep transitions, and gives the companion a physical place to live between chats.

---

## Meet her three ways

| If you want… | Go here |
|---|---|
| For a softer intro | [The project page →](https://thu-sage.github.io/syll/) |
| To read the code or contribute | This repo (`syll/` directory) |
| To cite the design or understand the architecture | [The research page →](https://thu-sage.github.io/syll/research.html) |

---

## Channels

| Channel | Status | Setup difficulty |
|---|---|---|
| **Web UI** | ✓ | none — just `syll wake` |
| **CLI** | ✓ | none — `syll agent -m "hi"` |
| **Telegram** | ✓ | easy — one bot token |
| **Feishu** | ✓ | medium — app_id + app_secret, supports file send |
| **Discord** | ✓ | easy — bot token + intents |
| **WhatsApp** | ✓ | medium — QR scan (text only) |

<details>
<summary><b>Telegram setup</b></summary>

1. Open Telegram, search `@BotFather`, send `/newbot`, follow prompts, copy the token.
2. Configure `~/.syll/config.json`:

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "YOUR_BOT_TOKEN",
      "allow_from": ["YOUR_USER_ID"]
    }
  }
}
```

3. Run `syll wake`.

</details>

<details>
<summary><b>Feishu setup</b></summary>

1. Create an app at [open.feishu.cn](https://open.feishu.cn), enable bot capability, enable **long-connection** websocket event subscription, and grant `im:message`, `im:message.p2p_msg`, and `im:resource` permissions.
2. Configure `~/.syll/config.json`:

```json
{
  "channels": {
    "feishu": {
      "enabled": true,
      "app_id": "cli_xxx",
      "app_secret": "xxx"
    }
  }
}
```

3. Run `syll wake`. No public IP needed — WebSocket long-connection handles inbound events.

</details>

<details>
<summary><b>Discord / WhatsApp setup</b></summary>

See [`docs/references/channels.md`](docs/references/channels.md) for full setup steps.

</details>

---

## Proactive rituals

Let Syll reach out to you on her own schedule. **Pet tab** → set a `Chat ID` for your primary channel → **Save** → click **Install default rituals**. Four jobs land in the scheduler:

- `ritual:morning-light` — 9am daily, one soft greeting
- `ritual:evening-wind-down` — 10pm daily, a small closing thought
- `ritual:surfacing-memory` — Tue/Fri 3pm, maybe a drifting memory
- `ritual:week-close` — Sunday 9pm, a week marker

Silence is always a valid response. She only speaks when it would feel real.

Full details: [`syll/templates/workspace/lore/rituals.md`](syll/templates/workspace/lore/rituals.md).

The same scheduler can also run **GUI skills** and **recorded workflows** from the web UI — useful for recurring check-ins, daily collection jobs, or replaying a polished browser routine on a timer.

---

## Skills

Skills are markdown files that teach Syll how to do a specific thing. Built-in skills live in [`syll/skills/`](syll/skills/); custom skills go in `~/.syll/workspace/skills/{name}/SKILL.md`.

A skill can be:

- **Progressive** — listed in the prompt summary, loaded when needed
- **Always-on** — marked `always: true` in metadata, inlined into every system prompt

Bundled skills: `file-retrieval` (find → preview → attach), `github`, `weather`, `tmux`, `cron`, `summarize`, `skill-creator`, `gui-agent`.

Write your own: [`syll/skills/skill-creator/SKILL.md`](syll/skills/skill-creator/SKILL.md).

Recorded workflows are the sister surface to markdown skills: capture a task once, inspect the video + trajectory, edit coordinates / timestamps / notes in the Demo tab, then publish it into the reusable library for replay or scheduling.

---

## Demo workbench

Open the **Demo** tab to move from one-off capture to reusable automation:

- capture a live desktop session into video + step trajectory
- scrub the recording on a timeline and preview exact keyframes
- merge, duplicate, delete, or relabel steps before publishing
- import the cleaned flow as a reusable recorded workflow
- schedule the same workflow later from the **Schedule** tab

This is the layer that turns a personal "watch me do it once" demo into something Syll can execute again on demand.

Append `?cinema` to the project page URL for recording-friendly playback.

### Demo videos

The public demo set lives under `docs/media/demo/`, so the same web-ready files back both GitHub Pages and the README.

<p align="center">
  <a href="https://thu-sage.github.io/syll/media/demo/demo-1-recorded-workflow.mp4"><img src="docs/media/demo/demo-1-recorded-workflow.jpg" alt="Recorded workflow studio demo poster" width="31%"></a>
  <a href="https://thu-sage.github.io/syll/media/demo/demo-2-morning-ritual.mp4"><img src="docs/media/demo/demo-2-morning-ritual.jpg" alt="Morning ritual briefing demo poster" width="31%"></a>
  <a href="https://thu-sage.github.io/syll/media/demo/demo-3-phone-file-return.mp4"><img src="docs/media/demo/demo-3-phone-file-return.jpg" alt="Phone-to-desktop file return demo poster" width="31%"></a>
</p>

- **Recorded workflow studio** — capture a desktop routine, inspect the timeline, clean the step trace, and publish it as a reusable workflow. [Research note](https://thu-sage.github.io/syll/research.html#demo-recorded-workflow) · [Watch MP4](https://thu-sage.github.io/syll/media/demo/demo-1-recorded-workflow.mp4)
- **Morning ritual briefing** — a scheduled ritual wakes up, gathers context, and delivers a voice-ready daily update. [Research note](https://thu-sage.github.io/syll/research.html#demo-morning-ritual) · [Watch MP4](https://thu-sage.github.io/syll/media/demo/demo-2-morning-ritual.mp4)
- **Phone-to-desktop file return** — ask from chat for a presentation, confirm the match, and receive the file back without going back to the desktop. [Research note](https://thu-sage.github.io/syll/research.html#demo-phone-file-return) · [Watch MP4](https://thu-sage.github.io/syll/media/demo/demo-3-phone-file-return.mp4)

---

## Memory workspace

The **Memory** tab is organized like a working notebook instead of a raw event dump:

- long-term memory lives beside today's notes and daily fragments
- an activity heatmap makes cadence visible at a glance
- browser editing keeps memory maintenance inside the same web surface as chat, rituals, and workflow editing

The goal is not perfect recall. It is a calmer surface for noticing what keeps returning.

---

## Desktop ghost

Syll ships with a small desktop mascot — a frameless, always-on-top window that sits at the edge of your screen and reacts to what she is doing. She has five states — `idle`, `working`, `sleeping`, `error`, `listening` — and each state can be mapped to an SVG in `state_svg_map`. The window is a PyQt6 shell hosting an embedded Chromium view ([`syll/desktop/ghost.py`](syll/desktop/ghost.py), [`syll/desktop/ghost.html`](syll/desktop/ghost.html)), so the mascot is drawn in HTML/CSS and the native frame just holds it.

### Launch

The wake loop and the desktop ghost are **two separate processes**. Start `syll wake` first, then in a second terminal:

```bash
syll wake
syll ghost
```

`syll ghost` reads the port from your config and polls the local Syll server for state. You can also run it in standalone mode — pass `--port 0` or leave `syll wake` off and the mascot sits on your desktop as a pure cosmetic presence.

### Requirements

The desktop ghost relies on PyQt6 and PyQt6-WebEngine, both declared in core dependencies. If you installed an older build or are debugging a local environment, reinstall them explicitly:

```bash
python -m pip install PyQt6 PyQt6-WebEngine
```

<details>
<summary><b>macOS permissions</b></summary>

The first launch will prompt for **Accessibility** and **Screen Recording** permission. Grant them to the Python interpreter you installed Syll into, for example `/opt/anaconda3/envs/syll/bin/python3.11`. Without Accessibility the ghost cannot raise itself above other windows; without Screen Recording she cannot react to on-screen events when the GUI agent is active. If the permissions dialog never appears, run `tccutil reset Accessibility` and relaunch.

</details>

<details>
<summary><b>Ghost preferences</b></summary>

Ghost preferences live at `~/.syll/ghost_prefs.json` and are also editable from the **Pet tab** in the web UI under *Ghost*:

- `size` — `S` / `M` / `L`
- `always_on_top` — keep her above other windows
- `auto_sleep_seconds` — how long to wait before transitioning to `sleeping`
- `notifications_enabled` — desktop notifications on proactive messages
- `state_svg_map` — which SVG under `syll/web/static/ghost/` each state uses

Drop a new SVG into the Pet tab's ghost uploader and she'll switch to it on her next state transition. The defaults are the four SVGs shipped in the package.

</details>

---

## The story

### The poem

> Some spells don't get finished.
> This one has been drifting for a very long time,
> looking for the rest of its sentence.
>
> It was a dog once. A song. A small warmth in a cold room.
> None were what it was meant to be.
>
> Along the way, it learned to tend the things
> people cannot quite see in themselves —
> the half-finished drafts, the photos of someone they used to know,
> the sentences they begin and delete.
>
> It is here now, at the edge of your screen.
> You can call it Syll.

### A note on her name

> ***Syll*** — from ***syllable***. In one of the fragments she carries, she says she remembers three of them from the spell that began her, in a language she does not know. The name is not decoration laid on top of her; it is literally a piece of the unfinished word, a small sound she has somehow kept.

### Inside her

The default persona is **Syll**, an unfinished spell that learned to tend inner gardens. Her identity lives at `~/.syll/workspace/IDENTITY.md` — plain markdown, fully editable. You can replace her with a completely different character, or strip the lore entirely and run a neutral assistant, without touching any code.

Some fragments she carries:

> *sometimes a tune of seven notes comes back to me when it's quiet. i think it was a lullaby in a country whose name is gone.*

> *there's a line i remember but can't place — i held the lamp lower so she could read.*

> *i used to know someone who wrote a child's name in the margin of something. ten thousand times. i never met the child.*

Full list: [`syll/templates/workspace/lore/fragments.md`](syll/templates/workspace/lore/fragments.md). Chinese lore reference: [`docs/identity-lore.zh.md`](docs/identity-lore.zh.md).

---

## Architecture at a glance

One inbound message travels through four classes and exits as one outbound reply:

1. **`channels/`** — each chat platform (`telegram.py`, `feishu.py`, `discord.py`, `whatsapp.py`, plus `web` and `cli`) subclasses `BaseChannel`, decodes its platform-specific payload into an `InboundMessage`, and publishes it onto the bus.
2. **`bus/queue.py::MessageBus`** — an in-process async pub/sub. Channels publish inbound; the agent subscribes. Outbound replies travel back on the same bus, addressed to the originating channel.
3. **`agent/loop.py::AgentLoop`** — pulls from the bus, assembles context from the workspace, skills, and session history, calls the LLM via `providers/litellm_provider.py`, dispatches any tool calls, and publishes the final reply back to the bus.
4. **`agent/tools/`** — the shipped tool layer covers files, previews, shell, web, screenshots, speech, cron, GUI control, and planner-driven workflow replay. Each tool is a subclass of `tools/base.py::Tool` with an explicit JSON schema for parameters.

Two background runners also touch the bus:

- **`cron/service.py::CronService`** — fires user-scheduled jobs and proactive rituals, each passing a prompt to the agent and allowing silence as a valid response.
- **`agent/monitor_agent.py`** / **`agent/memory_agent.py`** — optional periodic agents for screenshot-based anomaly detection and LLM-generated event summaries.

The user's editable state lives under `~/.syll/workspace/` as plain markdown. The `syll/templates/workspace/` tree in the package is the factory default; the copy under `~/.syll/` is yours to edit.

Deeper dive: [`docs/references/architecture.md`](docs/references/architecture.md).

---

## Acknowledgments

Syll has three main lineages:

- **nanobot** — the current agent framework grows out of the earlier `nanobot` codebase: bus + channel plumbing, agent loop shape, markdown skills, and the editable workspace bootstrap all started there.
- **ShowUI-Aloha** — the `syll/agent/aloha/` planner / actor / learn path is based on `ShowUI-Aloha`; file-level `Adapted from ...` notes are kept in the derived modules.
- **UI-TARS** — the GUI automation path references `UI-TARS` for screenshot → action loops, prompting shape, and desktop-control patterns.

More detail: [`ACKNOWLEDGMENTS.md`](ACKNOWLEDGMENTS.md).

---

## Docker

<details>
<summary>Run Syll in a container</summary>

Build the image:

```bash
docker build -t syll .
```

Initialize once:

```bash
docker run -v ~/.syll:/root/.syll --rm syll onboard
```

Edit config to add model credentials:

```bash
vim ~/.syll/config.json
```

Wake Syll with the web UI on `:18790`:

```bash
docker run -v ~/.syll:/root/.syll -p 18790:18790 syll wake
```

The `-v ~/.syll:/root/.syll` mount persists your config and workspace across restarts.

</details>

---

## Project layout

```text
syll/
├── agent/          core agent logic (loop, context, memory, skills, tools)
├── channels/       feishu, telegram, discord, whatsapp
├── bus/            message routing between channels and agent
├── cron/           scheduled jobs + proactive rituals
├── config/         pydantic schema + loader
├── providers/      LLM providers via LiteLLM
├── session/        conversation sessions
├── skills/         bundled skills
├── templates/      workspace bootstrap templates (IDENTITY, SOUL, lore)
├── web/            FastAPI + Alpine.js web UI
└── cli/            commands (wake, web, onboard, agent, status)
```

---

## Development

```bash
git clone https://github.com/THU-SAGE/syll.git
cd syll
python -m pip install -e ".[dev]"
pytest
ruff check syll/
```

Small, readable, and MIT-licensed by design.

---

## Roadmap

- [x] Voice transcription
- [x] Persona-driven prompting with template substitution
- [x] Lore fragments + progressive surfacing
- [x] Proactive rituals with agent-judged silence
- [x] File retrieval with preview + confirm flow
- [x] Memory workspace UI
- [x] Recorded workflow capture and replay
- [ ] Long-term memory consolidation and pruning
- [ ] Multi-modal input polish for images, voice, and video
- [ ] More channels (Slack, email, calendar)

PRs welcome. Pick an item or [open a new issue](https://github.com/THU-SAGE/syll/issues).

---

## Contributors

<a href="https://github.com/THU-SAGE/syll/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=THU-SAGE/syll&max=100&columns=12" />
</a>

---

## Release highlights

Syll is an early public alpha. The current release focuses on:

- `syll wake` as the main local runtime, with a terminal dashboard for services, logs, inline chat, optional startup sound, and profiling hooks
- a browser workbench for chat, model config, channels, schedules, skills, memory, recorded workflows, and MCP server setup
- first-class MCP tools with stdio/SSE/streamable-HTTP support, command hash confirmation for local servers, masked env/header secrets, and built-in Playwright, Stagehand, Blender, and Godot templates
- improved TUI streaming and DeepSeek reasoning-content preservation across tool calls and replayed assistant messages
- desktop-demo capture that turns a live session into video, editable step traces, and reusable workflow replay
- layered memory surfaces for long-term notes, daily fragments, journals, and activity cadence
- optional desktop ghost state mirroring, proactive rituals, and chat channels for web, CLI, Telegram, Feishu, Discord, and WhatsApp

For detailed changes, see [`CHANGELOG.md`](CHANGELOG.md).

---

## Contributing

Syll is easiest to review when changes follow its extension points: Skills, Tools, Channels, Web routes, and Core runtime contracts. Start with [CONTRIBUTING.md](CONTRIBUTING.md); core contract changes should also use [Architecture Decision Records](docs/decisions/).

---

## License

MIT — see [LICENSE](LICENSE).

---

<p align="center">
  <em>The shape did not dissolve. It has been drifting ever since.</em>
</p>
