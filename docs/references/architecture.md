# Architecture

## System Overview

```
┌────────────┐     ┌──────────┐     ┌────────────┐
│  Channels  │────▸│   Bus    │────▸│ Agent Loop │
│ TG/DC/WA/  │◂────│ In / Out │◂────│            │
│   Feishu   │     └──────────┘     │ ┌────────┐ │
└────────────┘                      │ │Context │ │
                                    │ │Builder │ │
                                    │ └────────┘ │
                                    │ ┌────────┐ │
                                    │ │  Tool  │ │
                                    │ │Registry│ │
                                    │ └────────┘ │
                                    │ ┌────────┐ │
                                    │ │Sessions│ │
                                    │ └────────┘ │
                                    └────────────┘
                                         │
                                    ┌────▼────┐
                                    │   LLM   │
                                    │Provider │
                                    │(LiteLLM)│
                                    └─────────┘
```

## Multimodal Data Flow

### Inbound (User → Agent)

```
Channel receives image
  → Downloads to ~/.syll/media/
  → InboundMessage(media=["/path/to/image.jpg"])
    → ContextBuilder._build_user_content()
      → base64 encode → image_url content part
        → LLM sees the image
```

### Tool Result (Tool → Agent)

```
Tool.execute() returns ToolResult(text="...", media=["/path/to/screenshot.png"])
  → ContextBuilder.add_tool_result()
    → base64 encode → image_url content part
      → LLM sees the screenshot in tool result
  → AgentLoop collects media paths in collected_media
```

### Outbound (Agent → User)

```
AgentLoop creates OutboundMessage(content="...", media=collected_media)
  → Channel.send()
    → Telegram: send_message() + send_photo() for each image
    → Discord: multipart POST with file attachments
```

## Tool System

```
Tool(ABC)
├── ReadFileTool
├── WriteFileTool
├── EditFileTool
├── ListDirTool
├── ExecTool
├── WebSearchTool
├── WebFetchTool
├── MessageTool
├── SpawnTool
├── CronTool
└── UITarsTool (new)
    └── Returns ToolResult with screenshots
```

## GUI Agent Flow

```
User: "Open Chrome and search for X"
  → AgentLoop calls gui_action tool
    → UITarsTool.execute()
      → Step 1: screenshot → UI-TARS API → "click Chrome icon" → pyautogui.click()
      → Step 2: screenshot → UI-TARS API → "click address bar" → pyautogui.click()
      → Step 3: screenshot → UI-TARS API → "type X" → pyautogui.typewrite()
      → Step 4: screenshot → UI-TARS API → "finished" → return ToolResult
    → LLM sees final screenshot + summary
      → Generates response to user
```
