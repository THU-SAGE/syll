"""Monitor Agent - Periodic screenshot analysis and anomaly detection."""

import asyncio
import tempfile
from datetime import datetime
from pathlib import Path

from loguru import logger

from syll.agent.events import Event, EventContent, EventSource, EventStore
from syll.bus.queue import MessageBus
from syll.providers.base import LLMProvider


class MonitorAgent:
    """Agent that periodically captures and analyzes screenshots."""

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        model: str | None = None,
        interval_minutes: int = 5,
    ):
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.interval_minutes = interval_minutes
        self.event_store = EventStore(workspace.parent)
        self._screenshot_dir = Path(tempfile.gettempdir()) / "syll_monitor"
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)

    async def capture_and_analyze(self) -> None:
        """Capture screenshot and analyze for anomalies."""
        try:
            # Take screenshot
            screenshot_path = await self._take_screenshot()
            if not screenshot_path:
                logger.warning("Failed to capture screenshot")
                return

            # Analyze screenshot
            analysis = await self._analyze_screenshot(screenshot_path)

            # Check for anomalies
            if analysis.get("has_anomaly"):
                await self._handle_anomaly(analysis, screenshot_path)

            # Log monitoring event
            event = Event(
                agent_type="monitor_agent",
                event_type="alert" if analysis.get("has_anomaly") else "memory",
                source=EventSource(platform="desktop", chat_id="monitor", user_id="system"),
                content=EventContent(
                    text=analysis.get("summary", "Routine monitoring check"),
                    media=[screenshot_path],
                    metadata={
                        "has_anomaly": analysis.get("has_anomaly", False),
                        "anomaly_type": analysis.get("anomaly_type"),
                    },
                ),
                tags=["monitor", "auto-generated"],
            )
            self.event_store.log_event(event)

        except Exception as e:
            logger.error(f"Monitor Agent error: {e}")

    async def _take_screenshot(self) -> str | None:
        """Take a screenshot and save to temp directory.

        Returns:
            Path to screenshot file or None if failed
        """
        try:
            import mss

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"monitor_{timestamp}.png"
            filepath = self._screenshot_dir / filename

            with mss.mss() as sct:
                monitor = sct.monitors[1]  # Primary monitor
                screenshot = sct.grab(monitor)
                mss.tools.to_png(screenshot.rgb, screenshot.size, output=str(filepath))

            logger.debug(f"Screenshot saved: {filepath}")
            return str(filepath)
        except Exception as e:
            logger.error(f"Failed to take screenshot: {e}")
            return None

    async def _analyze_screenshot(self, screenshot_path: str) -> dict:
        """Analyze screenshot for anomalies.

        Args:
            screenshot_path: Path to screenshot file

        Returns:
            Dict with analysis results
        """
        import base64

        try:
            # Read screenshot as base64
            with open(screenshot_path, "rb") as f:
                screenshot_b64 = base64.b64encode(f.read()).decode()

            # Build prompt
            prompt = """Analyze this screenshot and check for:
1. Error dialogs or warning messages
2. High CPU/memory usage indicators
3. Frozen or unresponsive applications
4. Security alerts or permission requests
5. Any other anomalies

Respond in this format:
Has Anomaly: yes/no
Anomaly Type: <type if yes, otherwise none>
Summary: <brief description>
"""

            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"},
                        },
                    ],
                }
            ]

            response = await self.provider.chat(messages=messages, model=self.model)
            text = response.content

            # Parse response
            has_anomaly = False
            anomaly_type = None
            summary = "Normal operation"

            for line in text.split("\n"):
                if line.startswith("Has Anomaly:"):
                    has_anomaly = "yes" in line.lower()
                elif line.startswith("Anomaly Type:"):
                    anomaly_type = line.replace("Anomaly Type:", "").strip()
                elif line.startswith("Summary:"):
                    summary = line.replace("Summary:", "").strip()

            return {
                "has_anomaly": has_anomaly,
                "anomaly_type": anomaly_type,
                "summary": summary,
            }
        except Exception as e:
            logger.error(f"Failed to analyze screenshot: {e}")
            return {"has_anomaly": False, "summary": "Analysis failed"}

    async def _handle_anomaly(self, analysis: dict, screenshot_path: str) -> None:
        """Handle detected anomaly by sending alert.

        Args:
            analysis: Analysis results
            screenshot_path: Path to screenshot
        """

        message = f"⚠️ Monitor Alert: {analysis['summary']}\nType: {analysis.get('anomaly_type', 'Unknown')}"

        # Send to all active channels (simplified - in production, use config)
        # For now, just log
        logger.warning(f"Anomaly detected: {message}")

        # You could publish to bus here if needed
        # await self.bus.publish_outbound(OutboundMessage(...))

    async def run_periodic(self):
        """Run periodic monitoring."""
        logger.info(f"Monitor Agent: Starting (every {self.interval_minutes} min)")

        while True:
            try:
                await self.capture_and_analyze()
            except Exception as e:
                logger.error(f"Monitor Agent error: {e}")

            await asyncio.sleep(self.interval_minutes * 60)
