# Channels

Syll connects to chat platforms through **channels**. Each channel is an adapter between a platform's message format and Syll's internal message bus. You can enable any subset in `~/.syll/config.json`.

| Channel | Status | Difficulty | Notes |
|---|---|---|---|
| Web UI | ✓ | none | ships with `syll wake` |
| CLI | ✓ | none | `syll agent -m "hi"` |
| Telegram | ✓ | easy | one bot token |
| Feishu | ✓ | medium | long-connection WebSocket, supports file send |
| Discord | ✓ | easy | bot token + intents |
| WhatsApp | ✓ | medium | QR scan via the Node bridge, text only |

---

## Telegram

1. Open Telegram, search `@BotFather`, send `/newbot`, follow prompts, copy the token.
2. Configure:
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

`allow_from` is an allowlist of Telegram user IDs — leave it empty only if you know what you're doing.

---

## Feishu (Lark)

1. Create an app at [open.feishu.cn](https://open.feishu.cn). Enable **bot** capability and **long-connection** (WebSocket) event subscription. Grant permissions: `im:message`, `im:message.p2p_msg`, `im:resource`.
2. Configure:
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
3. Run `syll wake`. No public IP needed — the WebSocket long-connection handles inbound events.

Feishu is the only bundled channel with first-class file send support (images, PDFs, etc.) via the `CreateImageRequest` and message resource APIs.

---

## Discord

1. Create an application at [discord.com/developers](https://discord.com/developers/applications). Add a bot, copy the bot token, enable the **Message Content** privileged intent.
2. Configure:
   ```json
   {
     "channels": {
       "discord": {
         "enabled": true,
         "token": "YOUR_BOT_TOKEN",
         "allow_from": ["YOUR_DISCORD_USER_ID"]
       }
     }
   }
   ```
3. Invite the bot to a server with the `bot` scope and `Send Messages` + `Read Message History` permissions.
4. Run `syll wake`.

---

## WhatsApp

WhatsApp is supported through a small Node bridge (in `bridge/`) that uses the Baileys library to talk to WhatsApp Web. Text only — no media send in this release.

1. Build the bridge:
   ```bash
   cd bridge
   npm install
   npm run build
   ```
2. Configure:
   ```json
   {
     "channels": {
       "whatsapp": {
         "enabled": true,
         "bridge_url": "ws://localhost:3001",
         "allow_from": ["1234567890@s.whatsapp.net"]
       }
     }
   }
   ```
3. Start the bridge in one terminal:
   ```bash
   cd bridge && npm start
   ```
   Scan the QR code with the phone's WhatsApp. The session is persisted under `~/.syll/whatsapp-auth/`.
4. Start Syll in another terminal: `syll wake`.

---

## Writing your own channel

A channel is a subclass of `BaseChannel` in `syll/channels/`. It needs to:

- Receive inbound messages and publish them to the bus (`InboundMessage`)
- Subscribe to outbound messages addressed to its channel name and deliver them to the platform
- Implement `start()` / `stop()` for lifecycle management

See `syll/channels/telegram.py` for the simplest example (≈120 lines).
