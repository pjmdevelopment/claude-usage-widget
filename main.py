"""
Claude Usage Widget
--------------------
A small always-on-top floating desktop widget (Windows, macOS, Linux) that
shows your live claude.ai / Claude Code subscription usage — the same
"Plan usage limits" data you see in the app:

  * Current session (5-hour window) utilisation + reset countdown
  * Weekly limits (all models, plus any model-scoped limits like Fable)

The data comes from the authenticated OAuth usage endpoint that Claude Code
itself uses:

    GET https://api.anthropic.com/api/oauth/usage

We authenticate with the OAuth access token that Claude Code has already
stored on this machine. On Windows/Linux that lives in a file
(``~/.claude/.credentials.json``); on macOS it lives in the login Keychain
(service ``Claude Code-credentials``). Either way the token is read FRESH on
every refresh, so when Claude Code rotates it in the background the widget just
keeps working — we never cache or copy the secret ourselves.

Run directly with:  python main.py
Build a standalone app with build.bat (Windows) or build.sh (macOS/Linux).

Widget config (position, refresh interval) is stored per-platform:
  Windows: %APPDATA%\\ClaudeUsageWidget\\config.json
  macOS:   ~/Library/Application Support/ClaudeUsageWidget/config.json
  Linux:   ~/.config/ClaudeUsageWidget/config.json
"""

import sys
import os
import json
import time
import threading
import subprocess
from datetime import datetime, timezone

import requests
from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QPainter, QColor, QFont, QAction, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QMenu,
    QSystemTrayIcon, QDialog, QFormLayout, QLineEdit, QSpinBox,
    QPushButton, QPlainTextEdit, QCheckBox,
)

APP_NAME = "ClaudeUsageWidget"
ANTHROPIC_VERSION = "2023-06-01"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# Where Claude Code keeps its OAuth tokens on Windows/Linux (a file). On macOS
# the token normally lives in the login Keychain instead — see read_oauth_token.
DEFAULT_CREDENTIALS_PATH = os.path.join(
    os.path.expanduser("~"), ".claude", ".credentials.json"
)

# macOS Keychain service name Claude Code stores its credentials under.
KEYCHAIN_SERVICE = "Claude Code-credentials"

# Severity -> bar colour. The API tells us the severity per limit so the
# colours track the app: blue when comfortable, amber when getting close,
# red when nearly exhausted.
SEVERITY_COLORS = {
    "normal": QColor(74, 158, 255),    # blue
    "warning": QColor(224, 160, 48),   # amber
    "critical": QColor(224, 85, 47),   # red-orange
}
DEFAULT_BAR_COLOR = QColor(74, 158, 255)


# --------------------------------------------------------------------------
# Config storage
# --------------------------------------------------------------------------

def config_dir():
    home = os.path.expanduser("~")
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or home
    elif IS_MAC:
        base = os.path.join(home, "Library", "Application Support")
    else:  # Linux / other Unix — follow the XDG base-dir spec.
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def config_path():
    return os.path.join(config_dir(), "config.json")


DEFAULT_CONFIG = {
    "refresh_seconds": 60,
    "pos_x": 60,
    "pos_y": 60,
    "credentials_path": DEFAULT_CREDENTIALS_PATH,
    "start_at_login": False,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    saved = {}
    if os.path.exists(config_path()):
        try:
            with open(config_path(), "r", encoding="utf-8") as f:
                saved = json.load(f)
        except Exception:
            saved = {}
    # Back-compat: earlier Windows-only builds used "start_with_windows".
    if "start_with_windows" in saved and "start_at_login" not in saved:
        saved["start_at_login"] = saved.pop("start_with_windows")
    cfg.update(saved)
    cfg.pop("start_with_windows", None)
    return cfg


def save_config(cfg):
    try:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# --------------------------------------------------------------------------
# "Start at login" — cross-platform. Windows uses the registry Run key,
# macOS a LaunchAgent plist, Linux an XDG autostart .desktop file.
# --------------------------------------------------------------------------

def _launch_argv():
    """The command needed to relaunch this app.

    When frozen by PyInstaller ``sys.executable`` is the app itself; otherwise
    we invoke the current Python interpreter on this script."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, os.path.abspath(__file__)]


def set_start_at_login(enabled: bool):
    try:
        if IS_WINDOWS:
            _set_autostart_windows(enabled)
        elif IS_MAC:
            _set_autostart_macos(enabled)
        elif IS_LINUX:
            _set_autostart_linux(enabled)
    except Exception:
        # Autostart is a convenience — never let it crash the app.
        pass


def _set_autostart_windows(enabled: bool):
    import winreg
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0, winreg.KEY_SET_VALUE,
    )
    if enabled:
        argv = _launch_argv()
        cmd = " ".join(f'"{a}"' for a in argv)
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
    else:
        try:
            winreg.DeleteValue(key, APP_NAME)
        except FileNotFoundError:
            pass
    winreg.CloseKey(key)


def _macos_plist_path():
    return os.path.join(
        os.path.expanduser("~"), "Library", "LaunchAgents",
        f"com.{APP_NAME.lower()}.plist",
    )


def _set_autostart_macos(enabled: bool):
    path = _macos_plist_path()
    if enabled:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        args_xml = "\n".join(
            f"        <string>{a}</string>" for a in _launch_argv()
        )
        plist = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            '<dict>\n'
            f'    <key>Label</key>\n    <string>com.{APP_NAME.lower()}</string>\n'
            '    <key>ProgramArguments</key>\n'
            f'    <array>\n{args_xml}\n    </array>\n'
            '    <key>RunAtLoad</key>\n    <true/>\n'
            '</dict>\n'
            '</plist>\n'
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(plist)
    elif os.path.exists(path):
        os.remove(path)


def _linux_desktop_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return os.path.join(base, "autostart", f"{APP_NAME}.desktop")


def _set_autostart_linux(enabled: bool):
    path = _linux_desktop_path()
    if enabled:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Quote each argument so paths with spaces survive the Exec line.
        exec_cmd = " ".join(f'"{a}"' for a in _launch_argv())
        entry = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Claude Usage Widget\n"
            f"Exec={exec_cmd}\n"
            "X-GNOME-Autostart-enabled=true\n"
            "Terminal=false\n"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(entry)
    elif os.path.exists(path):
        os.remove(path)


# --------------------------------------------------------------------------
# OAuth token access — read fresh from Claude Code's credentials file.
# --------------------------------------------------------------------------

def _read_keychain_credentials():
    """macOS only: pull Claude Code's OAuth JSON out of the login Keychain.
    Returns the parsed dict, or None if unavailable."""
    if not IS_MAC:
        return None
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return json.loads(out.stdout.strip())
    except Exception:
        return None


def read_oauth_token(credentials_path):
    """Return (token, error). Reads the source every call so a token rotated
    by Claude Code in the background is picked up automatically.

    Order of preference: the credentials file (Windows/Linux, and macOS if a
    file exists), then the macOS login Keychain."""
    data = None
    if os.path.exists(credentials_path):
        try:
            with open(credentials_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            return None, f"Can't read credentials: {e}"

    if data is None:
        data = _read_keychain_credentials()

    if data is None:
        if IS_MAC:
            return None, "Not logged in to Claude Code (no credentials file or Keychain entry)"
        return None, "Not logged in to Claude Code (no credentials file)"

    oauth = data.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        return None, "No OAuth access token in credentials file"

    # Warn (but still try) if the token looks expired — Claude Code normally
    # refreshes it whenever it runs.
    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)) and expires_at / 1000.0 < datetime.now(timezone.utc).timestamp():
        return token, "Token expired — run Claude Code once to refresh it"
    return token, None


# --------------------------------------------------------------------------
# Usage API client (runs in a background thread)
# --------------------------------------------------------------------------

# Bounds for how soon we auto-retry after a 429, in seconds.
RETRY_AFTER_MIN = 5
RETRY_AFTER_MAX = 60
RETRY_AFTER_DEFAULT = 15


def _parse_retry_after(response):
    """Seconds to wait before retrying a rate-limited request.

    Uses the ``Retry-After`` header when present (integer seconds), otherwise a
    sensible default, always clamped so we neither hammer the API nor stall."""
    raw = None
    try:
        raw = response.headers.get("Retry-After")
    except Exception:
        raw = None
    try:
        secs = int(float(raw)) if raw is not None else RETRY_AFTER_DEFAULT
    except (TypeError, ValueError):
        secs = RETRY_AFTER_DEFAULT
    return max(RETRY_AFTER_MIN, min(RETRY_AFTER_MAX, secs))


class UsageFetcher(QObject):
    # emits {"ok": bool, "error": str, "limits": [ ... ], "plan": str,
    #        "raw": str}
    finished = Signal(dict)

    def __init__(self, credentials_path: str):
        super().__init__()
        self.credentials_path = credentials_path

    def run(self):
        result = {"ok": False, "error": "", "limits": [], "plan": "", "raw": "",
                  "retry_after": None}

        token, err = read_oauth_token(self.credentials_path)
        if not token:
            result["error"] = err
            self.finished.emit(result)
            return
        # A soft warning (e.g. expired) still lets us try the request.
        soft_warning = err

        headers = {
            "Authorization": f"Bearer {token}",
            "anthropic-version": ANTHROPIC_VERSION,
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "ClaudeUsageWidget/2.0",
            "Accept": "application/json",
        }

        try:
            resp = requests.get(USAGE_URL, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            result["raw"] = json.dumps(data, indent=2)[:8000]
            result["limits"] = self._parse_limits(data)
            result["ok"] = True
            if soft_warning:
                result["error"] = soft_warning
        except requests.exceptions.HTTPError as e:
            body = ""
            try:
                body = e.response.text[:300]
            except Exception:
                pass
            code = e.response.status_code if e.response is not None else "?"
            if code == 401:
                result["error"] = "Auth rejected — run Claude Code to refresh login"
            elif code == 429:
                # Transient: the endpoint is rate-limiting us. Keep the last
                # good numbers on screen and quietly retry after the server's
                # hint (or a sensible default), capped so we never hammer.
                result["error"] = "Rate limited — retrying shortly"
                result["retry_after"] = _parse_retry_after(e.response)
            else:
                result["error"] = f"HTTP {code}: {body}"
        except Exception as e:
            result["error"] = f"{e}"

        self.finished.emit(result)

    @staticmethod
    def _parse_limits(data):
        """Turn the API's ``limits`` array into simple display rows.
        Falls back to the top-level five_hour/seven_day fields if needed."""
        rows = []

        def label_for(item):
            kind = item.get("kind", "")
            if kind == "session":
                return "Current session"
            if kind == "weekly_all":
                return "All models"
            if kind == "weekly_scoped":
                scope = item.get("scope") or {}
                model = (scope.get("model") or {}).get("display_name")
                return model or "Scoped weekly"
            return kind.replace("_", " ").title() or "Usage"

        limits = data.get("limits")
        if isinstance(limits, list) and limits:
            # Session limits first, then weekly, preserving API order within.
            order = {"session": 0, "weekly": 1}
            for item in sorted(limits, key=lambda i: order.get(i.get("group"), 2)):
                pct = item.get("percent")
                if pct is None:
                    continue
                rows.append({
                    "label": label_for(item),
                    "group": item.get("group", ""),
                    "percent": float(pct),
                    "severity": item.get("severity", "normal"),
                    "resets_at": item.get("resets_at"),
                })
            return rows

        # Fallback: older/simpler shape.
        for key, label, group in (
            ("five_hour", "Current session", "session"),
            ("seven_day", "All models", "weekly"),
        ):
            node = data.get(key) or {}
            if node.get("utilization") is not None:
                rows.append({
                    "label": label,
                    "group": group,
                    "percent": float(node["utilization"]),
                    "severity": "normal",
                    "resets_at": node.get("resets_at"),
                })
        return rows


# --------------------------------------------------------------------------
# A small custom progress bar (rounded, coloured by severity)
# --------------------------------------------------------------------------

class UsageBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._percent = 0.0
        self._color = DEFAULT_BAR_COLOR
        self.setFixedHeight(6)
        self.setMinimumWidth(120)

    def set_value(self, percent, color):
        self._percent = max(0.0, min(100.0, float(percent)))
        self._color = color
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        radius = r.height() / 2

        # Track
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 28))
        p.drawRoundedRect(r, radius, radius)

        # Fill
        if self._percent > 0:
            w = int(r.width() * self._percent / 100.0)
            w = max(w, r.height())  # keep the rounded cap visible for tiny values
            fill = r.adjusted(0, 0, -(r.width() - w), 0)
            p.setBrush(self._color)
            p.drawRoundedRect(fill, radius, radius)


# --------------------------------------------------------------------------
# One labelled usage row: title + "N% used" on top, bar below, reset text.
# --------------------------------------------------------------------------

class UsageRow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel("—")
        self.title.setStyleSheet("color: #E8E6E1; font-size: 12px; font-weight: 600;")
        self.pct = QLabel("")
        self.pct.setStyleSheet("color: #C9C6C0; font-size: 12px;")
        self.pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        top.addWidget(self.title)
        top.addStretch(1)
        top.addWidget(self.pct)
        layout.addLayout(top)

        self.bar = UsageBar()
        layout.addWidget(self.bar)

        self.reset = QLabel("")
        self.reset.setStyleSheet("color: #8A8782; font-size: 10px;")
        layout.addWidget(self.reset)

    def update_from(self, row):
        self.title.setText(row["label"])
        pct = row["percent"]
        self.pct.setText(f"{pct:.0f}% used")
        color = SEVERITY_COLORS.get(row.get("severity", "normal"), DEFAULT_BAR_COLOR)
        self.bar.set_value(pct, color)
        self.reset.setText(self._reset_text(row.get("resets_at")))

    @staticmethod
    def _reset_text(resets_at):
        if not resets_at:
            return ""
        try:
            target = datetime.fromisoformat(resets_at)
        except Exception:
            return ""
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        delta = target - datetime.now(timezone.utc)
        secs = int(delta.total_seconds())
        if secs <= 0:
            return "Resetting…"
        days, rem = divmod(secs, 86400)
        hours, rem = divmod(rem, 3600)
        mins, _ = divmod(rem, 60)
        if days > 0:
            return f"Resets in {days}d {hours}h"
        if hours > 0:
            return f"Resets in {hours}h {mins}m"
        return f"Resets in {mins}m"


# --------------------------------------------------------------------------
# Settings dialog
# --------------------------------------------------------------------------

class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Claude Usage Widget – Settings")
        self.cfg = cfg
        self.setMinimumWidth(420)

        layout = QFormLayout(self)

        self.refresh_spin = QSpinBox()
        self.refresh_spin.setRange(15, 3600)
        self.refresh_spin.setSingleStep(15)
        self.refresh_spin.setValue(cfg.get("refresh_seconds", 60))
        layout.addRow("Refresh every (seconds):", self.refresh_spin)

        self.creds_edit = QLineEdit(cfg.get("credentials_path", DEFAULT_CREDENTIALS_PATH))
        layout.addRow("Claude Code credentials:", self.creds_edit)

        note = QLabel(
            "Uses the OAuth login Claude Code already stored on this machine.\n"
            "If usage shows an auth error, run Claude Code once to refresh it."
        )
        note.setStyleSheet("color: #888; font-size: 11px;")
        layout.addRow(note)

        self.startup_check = QCheckBox("Start at login")
        self.startup_check.setChecked(cfg.get("start_at_login", False))
        layout.addRow(self.startup_check)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(cancel_btn)
        layout.addRow(btn_row)

    def apply_to_config(self):
        self.cfg["refresh_seconds"] = self.refresh_spin.value()
        self.cfg["credentials_path"] = self.creds_edit.text().strip() or DEFAULT_CREDENTIALS_PATH
        self.cfg["start_at_login"] = self.startup_check.isChecked()
        return self.cfg


# --------------------------------------------------------------------------
# The floating widget itself
# --------------------------------------------------------------------------

class UsageWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self._drag_pos = None
        self._last_raw = ""
        self._fetching = False
        self._cooldown_until = 0.0   # monotonic time before which we skip polling
        self._rows = []          # UsageRow widgets, reused across refreshes

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(280)
        self.move(self.cfg.get("pos_x", 60), self.cfg.get("pos_y", 60))

        self._build_ui()
        self._build_tray()

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.fetch_usage)
        self._apply_refresh_interval()

        # Re-render reset countdowns every 30s without hitting the API.
        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self._retick)
        self.tick_timer.start(30 * 1000)

        self.fetch_usage()

    # ---- UI -------------------------------------------------------------

    def _build_ui(self):
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(16, 14, 16, 14)
        self.root.setSpacing(10)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Plan usage limits")
        title.setStyleSheet("color: #E8E6E1; font-weight: 700; font-size: 13px;")
        self.plan_label = QLabel("")
        self.plan_label.setStyleSheet("color: #8A8782; font-size: 11px; font-weight: 600;")
        self.plan_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.plan_label)
        self.root.addLayout(header)

        # Container that holds the usage rows (rebuilt in _render_rows).
        self.rows_box = QVBoxLayout()
        self.rows_box.setContentsMargins(0, 0, 0, 0)
        self.rows_box.setSpacing(12)
        self.root.addLayout(self.rows_box)

        self.status_label = QLabel("Loading…")
        self.status_label.setStyleSheet("color: #8A8782; font-size: 10px;")
        self.root.addWidget(self.status_label)

    def _ensure_rows(self, n):
        """Make sure exactly n UsageRow widgets exist in rows_box."""
        while len(self._rows) < n:
            row = UsageRow()
            self._rows.append(row)
            self.rows_box.addWidget(row)
        for i, row in enumerate(self._rows):
            row.setVisible(i < n)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(24, 24, 24, 230))
        painter.setPen(QColor(255, 255, 255, 30))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 14, 14)

    # ---- Tray -------------------------------------------------------------

    def _build_tray(self):
        icon = self._make_icon()
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("Claude Usage Widget")

        menu = QMenu()
        refresh_action = QAction("Refresh now", self)
        refresh_action.triggered.connect(self.fetch_usage)
        menu.addAction(refresh_action)

        menu.addSeparator()

        self.hide_action = QAction("Hide widget", self)
        self.hide_action.triggered.connect(self.toggle_visibility)
        menu.addAction(self.hide_action)

        menu.addSeparator()

        settings_action = QAction("Settings...", self)
        settings_action.triggered.connect(self.open_settings)
        menu.addAction(settings_action)

        raw_action = QAction("Show last API response...", self)
        raw_action.triggered.connect(self.show_raw)
        menu.addAction(raw_action)

        menu.addSeparator()

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(QApplication.instance().quit)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _make_icon(self):
        pix = QPixmap(32, 32)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(214, 100, 60))
        p.setPen(Qt.NoPen)
        p.drawEllipse(2, 2, 28, 28)
        p.setPen(QColor(255, 255, 255))
        f = QFont()
        f.setBold(True)
        f.setPointSize(14)
        p.setFont(f)
        p.drawText(pix.rect(), Qt.AlignCenter, "C")
        p.end()
        return QIcon(pix)

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.toggle_visibility()

    def toggle_visibility(self):
        if self.isVisible():
            self.setVisible(False)
            self.refresh_timer.stop()
            self.hide_action.setText("Show widget")
        else:
            self.setVisible(True)
            self.hide_action.setText("Hide widget")
            self._apply_refresh_interval()
            self.fetch_usage()

    # ---- Dragging -----------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        self.cfg["pos_x"] = self.x()
        self.cfg["pos_y"] = self.y()
        save_config(self.cfg)

    def contextMenuEvent(self, event):
        self.tray.contextMenu().exec(event.globalPos())

    # ---- Fetch / render -------------------------------------------------

    def _apply_refresh_interval(self):
        secs = max(15, int(self.cfg.get("refresh_seconds", 60)))
        self.refresh_timer.start(secs * 1000)

    def fetch_usage(self):
        if self._fetching:
            return
        # Respect a rate-limit cooldown: after a 429 we wait out the server's
        # Retry-After before polling again — never poll faster to "recover".
        remaining = self._cooldown_until - time.monotonic()
        if remaining > 0:
            self.status_label.setText(f"⚠ Rate limited — waiting {int(remaining) + 1}s")
            return
        self._fetching = True
        self.status_label.setText("Refreshing…")

        fetcher = UsageFetcher(self.cfg.get("credentials_path", DEFAULT_CREDENTIALS_PATH))
        fetcher.finished.connect(self._on_usage_result)
        self._fetcher_ref = fetcher  # keep alive during the thread's life
        threading.Thread(target=fetcher.run, daemon=True).start()

    def _on_usage_result(self, result):
        self._fetching = False
        self._last_raw = result.get("raw", "") or self._last_raw

        if result["ok"]:
            self._last_limits = result.get("limits", [])
            self._render_rows(self._last_limits)
            now = datetime.now().strftime("%H:%M:%S")
            warn = result.get("error")
            self.status_label.setText(f"⚠ {warn}" if warn else f"Updated {now}")
        else:
            err = result.get("error", "Unknown error")
            self.status_label.setText(err[:70])
            # On a rate-limit, back OFF: suppress polling until the server's
            # Retry-After window elapses. The periodic timer keeps firing but
            # fetch_usage() will skip while we're inside the cooldown, so we
            # stop adding load instead of hammering. Last-known bars stay up.
            retry_after = result.get("retry_after")
            if retry_after:
                self._cooldown_until = time.monotonic() + retry_after

    def _render_rows(self, limits):
        self._ensure_rows(len(limits))
        for row_widget, row_data in zip(self._rows, limits):
            row_widget.update_from(row_data)
        self.adjustSize()

    def _retick(self):
        # Refresh just the countdown text from the last-known data.
        limits = getattr(self, "_last_limits", None)
        if limits:
            for row_widget, row_data in zip(self._rows, limits):
                row_widget.reset.setText(UsageRow._reset_text(row_data.get("resets_at")))

    def show_raw(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Last raw API response")
        dlg.resize(600, 500)
        layout = QVBoxLayout(dlg)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(self._last_raw or "(no data yet — try Refresh now)")
        layout.addWidget(text)
        dlg.exec()

    # ---- Settings ---------------------------------------------------------

    def open_settings(self):
        dlg = SettingsDialog(dict(self.cfg), self)
        if dlg.exec() == QDialog.Accepted:
            new_cfg = dlg.apply_to_config()
            self.cfg.update(new_cfg)
            save_config(self.cfg)
            set_start_at_login(self.cfg.get("start_at_login", False))
            self._apply_refresh_interval()
            self.fetch_usage()

    def closeEvent(self, event):
        self.cfg["pos_x"] = self.x()
        self.cfg["pos_y"] = self.y()
        save_config(self.cfg)
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    widget = UsageWidget()
    widget.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
