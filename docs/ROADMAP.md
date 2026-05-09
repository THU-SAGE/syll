# Syll Roadmap

## Completed

### v0.1 — Foundation
- [x] Agent loop with tool execution
- [x] Multi-channel support (Telegram, Discord, WhatsApp, Feishu)
- [x] Tool system (filesystem, shell, web, message, spawn, cron)
- [x] Skills system with progressive loading
- [x] Memory (long-term + daily notes)
- [x] Session management
- [x] Cron scheduling
- [x] Heartbeat service
- [x] Inbound multimodal (images from Telegram/Discord → LLM)

### v0.2 — Multimodal + GUI (Current)
- [x] `ToolResult` dataclass for multimodal tool returns
- [x] Context builder multimodal tool result support
- [x] Agent loop media collection + outbound
- [x] Telegram send_photo support
- [x] Discord multipart file upload
- [x] GUI config model (UITarsConfig, GuiConfig)
- [x] UI-TARS Tool implementation
- [x] GUI Agent Skill
- [x] Documentation system

## Planned

### v0.3 — Enhanced GUI
- [ ] Visual grounding verification (post-action screenshot check)
- [ ] OCR fallback for non-vision models
- [ ] Action recording and replay
- [ ] Browser automation integration (Playwright)

### v0.4 — Multi-Agent
- [ ] Agent-to-agent communication protocol
- [ ] Shared memory between agents
- [ ] Agent delegation and task routing
- [ ] Supervisor agent pattern

### v0.5 — Production Hardening
- [ ] Rate limiting per channel/user
- [ ] Token usage tracking and budgets
- [ ] Structured logging and metrics
- [ ] Health check endpoint
- [ ] Graceful shutdown with in-flight request draining
