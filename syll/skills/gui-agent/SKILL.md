---
name: gui-agent
description: Control desktop GUI applications via screenshots and automated actions.
homepage: https://github.com/bytedance/UI-TARS
metadata: {"syll":{"emoji":"🖥️","requires":{"bins":[],"env":["SYLL__TOOLS__GUI__ENABLED"]}}}
---

# GUI Agent

You can interact with desktop applications using the `gui_action` tool.

## How It Works

1. The tool captures a screenshot of the current screen
2. Sends it to the UI-TARS vision model for analysis
3. UI-TARS returns a thought + action (click, type, scroll, etc.)
4. The action is executed via pyautogui
5. Steps repeat until the task is complete or max steps reached

## Usage Protocol

When the user asks you to perform a GUI task:

1. **Understand the goal** - Break the task into clear steps
2. **Call `gui_action`** with a clear instruction
3. **Review the result** - Check the returned screenshot and status
4. **Iterate if needed** - Call again with refined instructions

## Example

User: "Open Chrome and search for 'Syll'"

You should call:
```
gui_action(instruction="Open Chrome browser, click on the address bar, type 'Syll' and press Enter")
```

## Safety Rules

1. **Never perform destructive actions** without user confirmation:
   - Deleting files or folders
   - Closing unsaved documents
   - System settings changes
   - Financial transactions
   - Sending messages/emails

2. **Always verify** the current screen state before acting

3. **Stop immediately** if the screen shows unexpected content (login pages with sensitive data, etc.)

4. **Report clearly** what actions were taken and their results

## Supported Actions

| Action | Description | Example |
|--------|-------------|---------|
| `click` | Left click at position | `click(point='<point>500 300</point>')` |
| `right_click` | Right click | `right_click(point='<point>500 300</point>')` |
| `double_click` | Double click | `double_click(point='<point>500 300</point>')` |
| `drag` | Drag from A to B | `drag(start='<point>100 100</point>', end='<point>200 200</point>')` |
| `type` | Type text | `type(content='hello world')` |
| `hotkey` | Key combination | `hotkey(key='ctrl+c')` |
| `scroll` | Scroll up/down | `scroll(point='<point>500 300</point>', direction='down', amount=3)` |
| `wait` | Pause | `wait(seconds=2)` |
| `finished` | Task complete | `finished(content='Done')` |

## Configuration

Enable in `config.json`:
```json
{
  "tools": {
    "gui": {
      "enabled": true,
      "ui_tars": {
        "api_base": "http://localhost:8000/v1",
        "api_key": "your-key",
        "model": "ui-tars"
      },
      "max_steps": 15,
      "confirm_destructive": true
    }
  }
}
```
