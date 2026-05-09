"""Platform-specific active window detection."""

import platform
import subprocess

_system = platform.system()


def get_active_window() -> str:
    """Return the title of the currently active window."""
    try:
        if _system == "Darwin":
            return _macos_active_window()
        if _system == "Windows":
            return _windows_active_window()
        if _system == "Linux":
            return _linux_active_window()
    except Exception:
        pass
    return ""


def _macos_active_window() -> str:
    script = (
        'tell application "System Events"\n'
        "    set frontApp to name of first application process whose frontmost is true\n"
        "    try\n"
        "        tell process frontApp\n"
        "            set winTitle to name of front window\n"
        "        end tell\n"
        '        return frontApp & " - " & winTitle\n'
        "    on error\n"
        "        return frontApp\n"
        "    end try\n"
        "end tell"
    )
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=2)
    return r.stdout.strip() if r.returncode == 0 else ""


def _windows_active_window() -> str:
    import ctypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    hwnd = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _linux_active_window() -> str:
    r = subprocess.run(
        ["xdotool", "getactivewindow", "getwindowname"],
        capture_output=True,
        text=True,
        timeout=2,
    )
    return r.stdout.strip() if r.returncode == 0 else ""
