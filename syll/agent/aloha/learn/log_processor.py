"""Log processor: converts raw GUI event logs into structured action sequences.

Adapted from ShowUI-Aloha/Aloha_Learn/log_processor.py.
"""

import copy
import json
import re
from pathlib import Path

from loguru import logger


class LogProcessor:
    def __init__(self):
        self.actions = []

    def timestamp_to_seconds(self, timestamp_str: str) -> float | None:
        """Convert timestamp string (HH:MM:SS.mmm) to seconds."""
        try:
            time_parts = timestamp_str.split(":")
            hours = int(time_parts[0])
            minutes = int(time_parts[1])
            seconds_parts = time_parts[2].split(".")
            seconds = int(seconds_parts[0])
            milliseconds = int(seconds_parts[1].ljust(3, "0")) if len(seconds_parts) > 1 else 0
            return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000.0
        except Exception as e:
            logger.warning(f"Error parsing timestamp: {timestamp_str}: {e}")
            return None

    def process_input_log(self, log_file_path: str | Path) -> list[dict]:
        """Process the input log file and extract actions."""
        log_file_path = Path(log_file_path)
        if not log_file_path.exists():
            raise FileNotFoundError(f"Log file not found: {log_file_path}")

        actions = []
        current_software = None
        coord_pattern = r'\((\d+),\s*(\d+)\)'

        with open(log_file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            try:
                event_data = json.loads(line)

                if "video_start_time" in event_data:
                    continue

                timestamp = event_data.get("timestamp")
                message = event_data.get("message")
                window = event_data.get("window")

                if window and "Screen Recorder" in window:
                    if isinstance(message, str):
                        if message.startswith("Initial Active Window") or message.startswith(
                            "Active Window"
                        ):
                            continue

                if message.startswith("Active Window"):
                    continue

                timestamp_seconds = self.timestamp_to_seconds(timestamp)
                if timestamp_seconds is None:
                    continue

                coords = []
                coord_matches = re.finditer(coord_pattern, message)
                for coord_match in coord_matches:
                    coords.append({"x": int(coord_match.group(1)), "y": int(coord_match.group(2))})

                if "Initial Active Window:" in message or "Active Window:" in message:
                    current_software = window
                    action = "Active Window"
                else:
                    action = message
                    if coords:
                        action = re.sub(r'\s*\(\d+,\s*\d+\)', '', action)

                if timestamp == "00:00:00.000" and current_software is None:
                    try:
                        config_data = json.loads(message)
                        actions.append({
                            "timestamp": timestamp_seconds,
                            "action": "CONFIG",
                            "coords": config_data,
                            "current_software": "System Info",
                        })
                        continue
                    except Exception:
                        pass

                actions.append({
                    "timestamp": timestamp_seconds,
                    "action": action,
                    "coords": coords if coords else None,
                    "current_software": window,
                })

            except json.JSONDecodeError:
                logger.warning(f"JSON parsing error: {line}")
                continue
            except Exception as e:
                logger.warning(f"Processing error: {line} — {e}")
                continue

        if not actions:
            raise ValueError("No valid actions found in log file")

        self.actions = actions
        return actions

    def calculate_backspace_deletions(self, duration: float, base_rate: int = 10) -> int:
        """Calculate how many characters to delete based on backspace duration."""
        if duration <= 0.05:
            return 1
        elif duration <= 0.5:
            return max(1, int(duration * base_rate))
        else:
            base_deletions = int(0.5 * base_rate)
            accelerated_time = duration - 0.5
            accelerated_deletions = int(accelerated_time * base_rate * 2)
            return base_deletions + accelerated_deletions

    def merge_keyboard_events(self, actions: list[dict], time_threshold: float = 5.0) -> list[dict]:
        """Merge consecutive keyboard events into typing actions with backspace handling."""
        merged_actions = []
        buffer = []
        last_timestamp = None
        current_window = None
        last_click_coords = None
        special_keys = {'ENTER'}

        i = 0
        while i < len(actions):
            action = actions[i]

            if action["action"] == "CONFIG":
                merged_actions.append(action)
                i += 1
                continue

            timestamp = action["timestamp"]
            message = action["action"]
            window = action["current_software"]

            if window:
                current_window = window

            if "Click" in message and action["coords"]:
                last_click_coords = action["coords"]

            if "BACKSPACE" in message and ("Key Press:" in message or "Hotkey:" in message):
                backspace_start = timestamp
                backspace_end = timestamp
                j = i + 1
                while j < len(actions):
                    next_action = actions[j]
                    next_message = next_action["action"]
                    if "Key Release:" in next_message and "BACKSPACE" in next_message:
                        backspace_end = next_action["timestamp"]
                        break
                    elif not (
                        "BACKSPACE" in next_message
                        and ("Key Press:" in next_message or "Hotkey:" in next_message)
                    ):
                        break
                    j += 1

                duration = backspace_end - backspace_start
                chars_to_delete = self.calculate_backspace_deletions(duration)

                if buffer:
                    if chars_to_delete >= len(buffer):
                        buffer = []
                    else:
                        buffer = buffer[:-chars_to_delete]

                i = j
                continue

            elif "Key Press:" in message:
                key = message.replace("Key Press:", "").strip()

                if key in special_keys:
                    if buffer:
                        merged_actions.append({
                            "timestamp": last_timestamp,
                            "action": f"Type: {''.join(buffer)}",
                            "coords": last_click_coords,
                            "current_software": current_window,
                        })
                        buffer = []
                    merged_actions.append({
                        "timestamp": timestamp,
                        "action": "Press ENTER",
                        "coords": last_click_coords,
                        "current_software": current_window,
                    })

                elif key == 'SPACE':
                    if (
                        last_timestamp
                        and (timestamp - last_timestamp) > time_threshold
                        and buffer
                    ):
                        merged_actions.append({
                            "timestamp": last_timestamp,
                            "action": f"Type: {''.join(buffer)}",
                            "coords": last_click_coords,
                            "current_software": current_window,
                        })
                        buffer = []
                    buffer.append(" ")
                    last_timestamp = timestamp
                    current_window = current_window or action.get("current_software")
                    if action.get("coords") is not None:
                        last_click_coords = action.get("coords")

                elif key in ('DELETE', 'BACKSPACE'):
                    if buffer:
                        merged_actions.append({
                            "timestamp": last_timestamp,
                            "action": f"Type: {''.join(buffer)}",
                            "coords": last_click_coords,
                            "current_software": current_window,
                        })
                        buffer = []
                    merged_actions.append({
                        "timestamp": timestamp,
                        "action": "Press " + key,
                        "coords": last_click_coords,
                        "current_software": current_window,
                    })

                elif len(key) == 1:
                    if last_timestamp and (timestamp - last_timestamp) > time_threshold and buffer:
                        merged_actions.append({
                            "timestamp": last_timestamp,
                            "action": f"Type: {''.join(buffer)}",
                            "coords": last_click_coords,
                            "current_software": current_window,
                        })
                        buffer = []
                    buffer.append(key)
                    last_timestamp = timestamp

            elif (
                "Hotkey: SHIFT+" in message
                and "BACKSPACE" not in message
                and len(message.replace("Hotkey: SHIFT+", "").strip()) == 1
            ):
                key = message.replace("Hotkey: SHIFT+", "").strip()
                buffer.append(key)
                last_timestamp = timestamp

            elif message.startswith("Hotkey: CTRL+"):
                merged_actions.append({
                    "timestamp": timestamp,
                    "action": message,
                    "coords": last_click_coords,
                    "current_software": current_window,
                })

            elif (
                "Key Release:" in message
                or message == "Hotkey: SHIFT"
                or ("Hotkey:" in message and "BACKSPACE" not in message)
            ):
                pass

            else:
                if buffer:
                    merged_actions.append({
                        "timestamp": last_timestamp,
                        "action": f"Type: {''.join(buffer)}",
                        "coords": last_click_coords,
                        "current_software": current_window,
                    })
                    buffer = []
                merged_actions.append(action)

            i += 1

        if buffer:
            merged_actions.append({
                "timestamp": last_timestamp,
                "action": f"Type: {''.join(buffer)}",
                "coords": last_click_coords,
                "current_software": current_window,
            })

        return merged_actions

    def merge_mouse_events(self, actions: list[dict]) -> list[dict]:
        """Merge click and release events."""

        def coords_close(a, b, tol=3):
            if a is None or b is None:
                return False
            return max(abs(a[0] - b[0]), abs(a[1] - b[1])) <= tol

        merged_actions = []
        i = 0

        while i < len(actions):
            current_action = actions[i]
            message = current_action["action"]

            if "Click" in message and current_action["coords"]:
                coords = current_action["coords"][0]
                click_coords = (coords["x"], coords["y"])

                if i + 1 < len(actions):
                    next_action = actions[i + 1]
                    if "Release" in next_action["action"] and next_action.get("coords"):
                        r0 = next_action["coords"][0]
                        rel_coords = (r0["x"], r0["y"])
                        if coords_close(click_coords, rel_coords, 5):
                            merged_actions.append({
                                "timestamp": next_action["timestamp"],
                                "action": message,
                                "coords": current_action["coords"],
                                "current_software": current_action.get("current_software"),
                            })
                            i += 2
                            continue

                if i + 2 < len(actions):
                    mid_action = actions[i + 1]
                    rel_action = actions[i + 2]
                    if (
                        "Active Window" in mid_action["action"]
                        and "Release" in rel_action["action"]
                        and rel_action.get("coords")
                    ):
                        r0 = rel_action["coords"][0]
                        rel_coords = (r0["x"], r0["y"])
                        if coords_close(click_coords, rel_coords, 5):
                            merged_actions.append({
                                "timestamp": rel_action["timestamp"],
                                "action": message,
                                "coords": current_action["coords"],
                                "current_software": current_action.get("current_software"),
                            })
                            i += 3
                            continue
            merged_actions.append(current_action)
            i += 1

        return merged_actions

    def process_scroll_events(self, actions: list[dict]) -> list[dict]:
        """Group consecutive scroll events."""
        processed_actions = []
        i = 0

        while i < len(actions):
            current_action = actions[i]
            message = current_action["action"]

            if "Scroll" in message:
                scroll_down_count = 0
                scroll_up_count = 0
                coords_events = []

                if "ScrollDown" in message:
                    scroll_down_count += 1
                elif "ScrollUp" in message:
                    scroll_up_count += 1

                coords_events.append((
                    current_action["timestamp"],
                    current_action["coords"][0] if current_action["coords"] else None,
                ))

                while i + 1 < len(actions) and "Scroll" in actions[i + 1]["action"]:
                    i += 1
                    next_action = actions[i]
                    if "ScrollDown" in next_action["action"]:
                        scroll_down_count += 1
                    elif "ScrollUp" in next_action["action"]:
                        scroll_up_count += 1
                    coords_events.append((
                        next_action["timestamp"],
                        next_action["coords"][0] if next_action["coords"] else None,
                    ))

                coords_count = {}
                for timestamp, coord in coords_events:
                    if coord:
                        coord_str = f"{coord['x']},{coord['y']}"
                        if coord_str in coords_count:
                            coords_count[coord_str][0] += 1
                        else:
                            coords_count[coord_str] = [1, timestamp, coord]

                max_count = 0
                best_timestamp = current_action["timestamp"]
                best_coord = current_action["coords"][0] if current_action["coords"] else None

                for _, (count, timestamp, coord) in coords_count.items():
                    if count > max_count:
                        max_count = count
                        best_timestamp = timestamp
                        best_coord = coord

                if scroll_down_count >= scroll_up_count:
                    scroll_action = "ScrollDown"
                    scroll_count = scroll_down_count - scroll_up_count
                else:
                    scroll_action = "ScrollUp"
                    scroll_count = scroll_up_count - scroll_down_count

                processed_actions.append({
                    "timestamp": best_timestamp,
                    "action": f"{scroll_action} at",
                    "coords": [best_coord] if best_coord else None,
                    "current_software": current_action["current_software"],
                    "scroll_count": scroll_count,
                })
            else:
                processed_actions.append(current_action)

            i += 1

        return processed_actions

    def merge_adjacent_typing(self, actions: list[dict], time_threshold: float = 5.0) -> list[dict]:
        """Merge adjacent typing actions that are close in time and location."""
        if not actions:
            return actions

        merged_actions = []
        i = 0

        while i < len(actions):
            current_action = actions[i]

            if current_action["action"].startswith("Type:"):
                typing_sequence = [current_action]
                j = i + 1

                while j < len(actions):
                    next_action = actions[j]
                    if not next_action["action"].startswith("Type:"):
                        break
                    time_gap = next_action["timestamp"] - typing_sequence[-1]["timestamp"]
                    if time_gap > time_threshold:
                        break
                    if (
                        current_action["coords"]
                        and next_action["coords"]
                        and abs(current_action["coords"][0]["x"] - next_action["coords"][0]["x"]) <= 10
                        and abs(current_action["coords"][0]["y"] - next_action["coords"][0]["y"]) <= 10
                    ):
                        typing_sequence.append(next_action)
                        j += 1
                    else:
                        break

                if len(typing_sequence) > 1:
                    combined_text = "".join(
                        ta["action"].replace("Type: ", "") for ta in typing_sequence
                    )
                    merged_actions.append({
                        "timestamp": typing_sequence[-1]["timestamp"],
                        "action": f"Type: {combined_text}",
                        "coords": typing_sequence[0]["coords"],
                        "current_software": typing_sequence[0]["current_software"],
                    })
                    i = j
                else:
                    merged_actions.append(current_action)
                    i += 1
            else:
                merged_actions.append(current_action)
                i += 1

        merged_actions = [
            a for a in merged_actions if "Active Window" not in (a.get("action") or "")
        ]
        return merged_actions

    def merge_drag_events(self, actions: list[dict]) -> list[dict]:
        """Merge DragStart → DragMove → DragEnd sequences."""
        merged = []
        i = 0
        while i < len(actions):
            a0 = actions[i]
            msg0 = a0.get("action") or ""
            if msg0.startswith("DragStart") and a0.get("coords"):
                start_coord = a0["coords"][0] if a0["coords"] else None
                path = [start_coord]
                j = i + 1
                end_action = None
                while j < len(actions):
                    aj = actions[j]
                    msgj = aj.get("action") or ""
                    if msgj.startswith("DragMove") and aj.get("coords"):
                        path.append(aj["coords"][0])
                        j += 1
                        continue
                    if "Active Window" in msgj:
                        j += 1
                        continue
                    if "DragEnd" in msgj:
                        path.append(aj["coords"][0])
                        start_out = {
                            "timestamp": a0["timestamp"],
                            "action": "DragStart at",
                            "coords": [start_coord] if start_coord else None,
                            "current_software": a0.get("current_software"),
                        }
                        if path:
                            start_out["path"] = path
                        merged.append(start_out)
                        i = j + 1
                        end_action = True
                        break
                    break
                if end_action:
                    continue
                merged.append(a0)
                i += 1
                continue
            merged.append(a0)
            i += 1
        return merged

    def cleanup_preceded_double_clicks(self, actions: list[dict], tol: int = 5) -> list[dict]:
        """Remove single click immediately preceding a nearby double-click."""

        def get_xy(act):
            try:
                c = act.get("coords") or []
                if not c:
                    return None
                return (int(c[0]["x"]), int(c[0]["y"]))
            except Exception:
                return None

        def is_click(act):
            msg = (act.get("action") or "").lower()
            return ("click" in msg) and ("dblclick" not in msg) and ("double" not in msg)

        def is_dblclick(act):
            msg = (act.get("action") or "").lower()
            return ("dblclick" in msg) or ("doubleclick" in msg) or ("double click" in msg)

        def close(a, b, t=tol):
            if a is None or b is None:
                return False
            return max(abs(a[0] - b[0]), abs(a[1] - b[1])) <= t

        out = []
        for act in actions:
            if is_dblclick(act) and out:
                prev = out[-1]
                if is_click(prev) and close(get_xy(prev), get_xy(act), tol):
                    out.pop()
                    out.append(act)
                    continue
            out.append(act)
        return out

    def cleanup_click_before_drag(self, actions: list[dict], tol: int = 5) -> list[dict]:
        """Remove single click immediately preceding a nearby drag start."""

        def get_xy(act):
            try:
                c = act.get("coords") or []
                if not c:
                    return None
                return (int(c[0]["x"]), int(c[0]["y"]))
            except Exception:
                return None

        def is_lclick(act):
            msg = (act.get("action") or "").lower()
            return ("lclick" in msg or "left click" in msg) and ("double" not in msg)

        def is_dragstart(act):
            msg = (act.get("action") or "").lower()
            return msg.startswith("dragstart")

        def close(a, b):
            if a is None or b is None:
                return False
            return max(abs(a[0] - b[0]), abs(a[1] - b[1])) <= tol

        out = []
        for act in actions:
            if is_dragstart(act) and out and is_lclick(out[-1]) and close(get_xy(out[-1]), get_xy(act)):
                out.pop()
            out.append(act)
        return out

    def process_log_file(
        self, log_file_path: str | Path, output_path: str | Path | None = None,
        time_threshold: float = 5.0,
    ) -> list[dict]:
        """Main processing pipeline."""
        actions = self.process_input_log(log_file_path)
        logger.info(f"Parsed {max(0, len(actions) - 1)} raw actions")
        config_action = next(
            (copy.deepcopy(action) for action in actions if (action.get("action") or "") == "CONFIG"),
            None,
        )

        actions = self.merge_keyboard_events(actions, time_threshold)
        actions = self.merge_mouse_events(actions)
        actions = self.merge_drag_events(actions)
        actions = self.process_scroll_events(actions)
        actions = self.merge_adjacent_typing(actions, time_threshold)
        actions = self.cleanup_preceded_double_clicks(actions, tol=5)
        actions = self.cleanup_click_before_drag(actions, tol=5)

        actions.sort(key=lambda a: a.get("timestamp", 0))
        if config_action and not any((action.get("action") or "") == "CONFIG" for action in actions):
            actions.insert(0, config_action)

        if output_path:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(actions, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            logger.info(f"Processed log saved to: {output_path}")

        logger.info(f"Final action count: {len(actions)}")
        return actions
