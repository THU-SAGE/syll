"""Claude Computer Use agent for Aloha Actor backend.

Adapted from ShowUI-Aloha/Aloha_Act/ui_aloha/act/gui_agent/actor/agents/claude_computer_use_agent.py.
"""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from loguru import logger

PROMPT_TEMPLATES_DIR = Path(__file__).parent / "prompt_templates"


class ClaudeComputerUseAgent:
    """Claude Computer Use agent that generates GUI actions."""

    # Action type conversion mapping
    ACTION_TYPE_MAP = {
        "left_click": "CLICK",
        "double_click": "DOUBLE_CLICK",
        "triple_click": "TRIPLE_CLICK",
        "mouse_move": "MOVE",
        "scroll": "SCROLL",
        "wait": "WAIT",
        "key": "KEY",
        "type": "TYPE",
        "left_click_drag": "DRAG",
        "keypress": "KEY",
    }

    def __init__(self, api_key: str, display_width: int = 1024, display_height: int = 768):
        self.api_key = api_key
        self.display_width = display_width
        self.display_height = display_height
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=self.api_key)
            except ImportError:
                raise RuntimeError("anthropic package required for Claude CUA. Install: pip install anthropic")
        return self._client

    def execute(
        self,
        instruction: str,
        screenshot_b64: str,
        os_name: str = "macOS",
    ) -> tuple[dict, bool]:
        """Execute Claude Computer Use to get next action.

        Args:
            instruction: Task instruction for this step.
            screenshot_b64: Base64 encoded screenshot (no prefix).
            os_name: Operating system name.

        Returns:
            Tuple of (action_dict, is_complete).
            action_dict has keys: action, value, position.
        """
        try:
            from anthropic.types.beta import BetaTextBlock, BetaToolUseBlock
        except ImportError:
            return {"action": "ERROR", "value": "anthropic not available", "position": [0, 0]}, False

        client = self._get_client()

        # Build system prompt
        env = Environment(
            loader=FileSystemLoader(str(PROMPT_TEMPLATES_DIR)),
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        system_prompt = env.get_template("actor/system_cua.txt").render(os_name=os_name)
        user_text = env.get_template("actor/user_cua.txt").render(task=instruction)

        try:
            response = client.beta.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=512,
                tools=[{
                    "type": "computer_20250124",
                    "name": "computer",
                    "display_width_px": self.display_width,
                    "display_height_px": self.display_height,
                    "display_number": 1,
                }],
                system=system_prompt,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": screenshot_b64,
                            },
                        },
                        {"type": "text", "text": user_text},
                    ],
                }],
                betas=["computer-use-2025-01-24"],
            )

            return self._parse_response(response, BetaTextBlock, BetaToolUseBlock)

        except Exception as e:
            logger.error(f"Claude CUA error: {e}")
            return {"action": "ERROR", "value": str(e), "position": [0, 0]}, False

    def _parse_response(self, response, BetaTextBlock, BetaToolUseBlock) -> tuple[dict, bool]:
        """Parse Claude Computer Use response into standardized action format."""
        action_json = {}
        computer_call_found = False

        for item in response.content:
            if isinstance(item, BetaTextBlock):
                logger.debug(f"Claude CUA reasoning: {item.text}")
            elif isinstance(item, BetaToolUseBlock):
                action = item.input
                action_type = action.get("action", "")

                if action_type in self.ACTION_TYPE_MAP:
                    computer_call_found = True
                    coord = action.get('coordinate', [0, 0])

                    action_json = {
                        "action": self.ACTION_TYPE_MAP[action_type],
                        "value": action.get("text", ""),
                        # Keep raw coordinate in Claude computer tool API space.
                        # It will be converted by CoordinateTransformService.
                        "position": [int(coord[0]), int(coord[1])],
                    }

                    # Handle type/key actions
                    if action_type in ("type", "key", "keypress"):
                        action_json["value"] = action.get("text", "")
                        action_json["position"] = [0, 0]

        if not computer_call_found:
            action_json = {"action": "ERROR", "value": "No valid action found", "position": [0, 0]}

        return action_json, action_json.get("action") == "STOP"
