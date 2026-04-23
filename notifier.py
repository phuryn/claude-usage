"""
notifier.py - Cross-platform OS notifications with cooldown dedup.
"""

import platform
import subprocess
import time

_last_fired: dict[str, float] = {}


def send_notification(title: str, message: str, cooldown_minutes: int = 10, alert_key: str = None) -> bool:
    key = alert_key or f"{title}:{message}"
    now = time.time()
    cooldown_secs = cooldown_minutes * 60
    if key in _last_fired and now - _last_fired[key] < cooldown_secs:
        return False
    _last_fired[key] = now
    _dispatch(title, message)
    return True


def _dispatch(title: str, message: str) -> None:
    system = platform.system()
    try:
        if system == "Darwin":
            script = f'display notification "{_esc(message)}" with title "{_esc(title)}"'
            subprocess.run(["osascript", "-e", script], timeout=5, capture_output=True)
        elif system == "Windows":
            _windows_balloon(title, message)
        else:
            subprocess.run(["notify-send", title, message], timeout=5, capture_output=True)
    except Exception:
        pass


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _windows_balloon(title: str, message: str) -> None:
    safe_title = _esc(title)
    safe_msg = _esc(message)
    ps_script = f"""
Add-Type -AssemblyName System.Windows.Forms
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.BalloonTipTitle = "{safe_title}"
$n.BalloonTipText = "{safe_msg}"
$n.Visible = $true
$n.ShowBalloonTip(5000)
Start-Sleep -Milliseconds 500
$n.Dispose()
"""
    subprocess.run(
        ["powershell", "-NonInteractive", "-Command", ps_script],
        timeout=15,
        capture_output=True,
    )
