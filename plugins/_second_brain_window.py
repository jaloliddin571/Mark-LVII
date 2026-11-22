"""
Shared driver for second_brain: the galaxy window.

Not a skill (the leading underscore makes the loader skip it). Used by
second_brain.py (voice / text commands, from a worker thread), by the 🧠 button
in the HUD header (Qt thread) and by the global Ctrl+Shift+B hotkey (its own
thread). All of them end up in show() / hide() / toggle() here.

THREADS
    Tools run in executor threads, and Qt widgets may only be touched from the
    GUI thread. on_ui() hands a function to the GUI thread through a queued
    signal and waits for its result, so a tool can report what it OBSERVED
    (the window is visible) rather than what it asked for.

WHERE IT OPENS
    "Inside the HUD" (the default): in place of the avatar, as one more screen
    of the HUD's centre stack next to the camera and the video, so it is part
    of Jarvis rather than a window beside it. Closing it gives the avatar back.
    "Second monitor": its own window, full screen on the monitor the HUD is not
    on, or beside the HUD when there is only one.
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
from urllib.parse import urlencode

from PyQt6.QtCore import QObject, Qt, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QGuiApplication, QIcon
from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget

_EARLY_MARK = "import PyQt6.QtWebEngineWidgets"
_EARLY_BLOCK = '''
# The second_brain plugin draws its galaxy in a web view, and Qt only allows
# QtWebEngine to be loaded BEFORE the QApplication exists, which is long before
# any plugin is imported. Loading it here costs nothing if it is not installed.
# (Added automatically by plugins/_second_brain_window.py.)
try:
    import PyQt6.QtWebEngineWidgets  # noqa: F401
except Exception:
    pass
'''


def _enable_early_webengine() -> str:
    """
    Makes ui.py load QtWebEngine before the QApplication exists, so the galaxy
    can open inside Jarvis from the next start on. Plugins are imported after
    the app is created, too late to do it from here. Adds the block right after
    ui.py's first top-level PyQt6 import, keeps ui.py.bak, and never touches a
    ui.py that already has it. Returns a short note for the log and the user.
    """
    import ast
    import shutil
    ui = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui.py")
    try:
        with open(ui, encoding="utf-8") as f:
            src = f.read()
        if _EARLY_MARK in src:
            return "ui.py is already set up; restart Jarvis once"
        after = None
        for node in ast.parse(src).body:
            mod = node.module if isinstance(node, ast.ImportFrom) else (
                node.names[0].name if isinstance(node, ast.Import) else "")
            if (mod or "").startswith("PyQt6"):
                after = node.end_lineno
                break
        if after is None:
            return "could not find where ui.py imports PyQt6; add the lines by hand (see the guide)"
        lines = src.splitlines(keepends=True)
        new = "".join(lines[:after]) + _EARLY_BLOCK + "".join(lines[after:])
        compile(new, ui, "exec")
        if not os.path.exists(ui + ".bak"):
            shutil.copy2(ui, ui + ".bak")
        tmp = ui + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(new)
        os.replace(tmp, ui)
        return "ui.py was set up just now; restart Jarvis once and it will open inside"
    except Exception as e:
        return f"could not set up ui.py ({e}); add the lines by hand (see the guide)"


try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    HAVE_WEBENGINE = True
    WEBENGINE_PROBLEM = ""
except Exception as _e:               # open in the browser instead
    QWebEngineView = None
    HAVE_WEBENGINE = False
    if "QCoreApplication" in str(_e) or "AA_ShareOpenGLContexts" in str(_e):
        # Installed, but this process loaded it too late: fix ui.py for next time.
        WEBENGINE_PROBLEM = _enable_early_webengine()
    else:
        WEBENGINE_PROBLEM = "PyQt6-WebEngine is not installed"
    print(f"[SecondBrain] the galaxy opens in the browser for now: {WEBENGINE_PROBLEM}")

from plugins import _second_brain_index as index

NAMESPACE = "second_brain"
PAGE = os.path.join(index.WEB_DIR, "brain.html")
HOTKEY_LABEL = "Ctrl+Shift+B"


def _log(msg: str) -> None:
    print(f"[SecondBrain] {msg}")


# ------------------------------------------------------------------ the vault
_vault: index.Vault | None = None


def vault_path() -> str:
    try:
        from memory.config_manager import get_plugin_config
        return (get_plugin_config(NAMESPACE).get("vault_path") or "").strip().strip('"')
    except Exception:
        return ""


def get_vault() -> index.Vault | None:
    """The vault from ⚙ → PLUGIN SETTINGS → Second Brain, re-opened if the path changed."""
    global _vault
    path = vault_path()
    if not path or not os.path.isdir(path):
        return None
    if _vault is None or os.path.normcase(_vault.path) != os.path.normcase(os.path.abspath(path)):
        _vault = index.Vault(path)
    _vault.refresh()
    return _vault


# ------------------------------------------------------------------ GUI thread
class _Bridge(QObject):
    call = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        # A real slot and an explicitly queued connection: a plain Python method would run on
        # whichever thread made the connection (a worker), and Qt aborts on widgets made there.
        self.call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @pyqtSlot(object)
    def _run(self, job):
        job()


_bridge: _Bridge | None = None
_bridge_lock = threading.Lock()


def on_ui(fn, timeout: float = 5.0):
    """Runs fn() on the Qt GUI thread and returns its result (None on timeout)."""
    app = QApplication.instance()
    if app is None:
        return None
    if threading.current_thread() is threading.main_thread():   # Qt's GUI thread is Python's main thread
        return fn()
    global _bridge
    with _bridge_lock:
        if _bridge is None:
            _bridge = _Bridge()
            _bridge.moveToThread(app.thread())
    done, box = threading.Event(), {}

    def job():
        try:
            box["value"] = fn()
        except Exception as e:
            box["error"] = e
        done.set()

    _bridge.call.emit(job)
    if not done.wait(timeout):
        return None
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ------------------------------------------------------------------ the window
def _hud():
    app = QApplication.instance()
    for w in app.topLevelWidgets() if app else []:
        if type(w).__name__ == "MainWindow":
            return w
    return None


def _accent() -> str:
    try:
        from memory.config_manager import load_api_keys
        return (load_api_keys().get("ui_color") or "").strip() or "#00d4ff"
    except Exception:
        return "#00d4ff"


def _assistant() -> str:
    try:
        from memory.config_manager import get_assistant_name
        return get_assistant_name()
    except Exception:
        return "Jarvis"


class BrainView(QWidget):
    """The galaxy page in a web view: embedded in the HUD's centre stack, or as its own window."""

    def __init__(self, embedded: bool):
        if embedded:
            super().__init__(None)
        else:
            super().__init__(None, Qt.WindowType.Window)
            self.setWindowTitle("Second Brain")
            icon = os.path.join(os.path.dirname(index.HERE), "config", "jarvis.ico")
            if os.path.exists(icon):
                self.setWindowIcon(QIcon(icon))
        self.embedded = embedded
        self.setStyleSheet("background: #0d0a07;")    # dark like both themes, so opening never flashes
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.view = QWebEngineView(self)
        self.view.page().setBackgroundColor(QColor("#0d0a07"))
        lay.addWidget(self.view)
        self.loaded = False
        self._queue: list[str] = []
        self.view.loadFinished.connect(self._on_loaded)
        self.view.titleChanged.connect(self._on_title)   # Esc in the page asks to close
        self.reload()

    def reload(self):
        self.loaded = False
        query = urlencode({"color": _accent(), "name": _assistant()})
        url = QUrl.fromLocalFile(PAGE)
        url.setQuery(query)
        self.view.load(url)

    def _on_loaded(self, ok: bool):
        self.loaded = ok
        if not ok:
            _log("the galaxy page failed to load")
            return
        for code in self._queue:
            self.view.page().runJavaScript(code)
        self._queue.clear()

    def _on_title(self, title: str):
        if title.startswith("close-requested:"):
            hide()

    def js(self, code: str):
        if self.loaded:
            self.view.page().runJavaScript(code)
        else:
            self._queue.append(code)

    def place(self) -> str:
        """Window mode: full screen on a monitor the HUD is not on; otherwise beside the HUD."""
        hud = _hud()
        hud_screen = hud.screen() if hud else QGuiApplication.primaryScreen()
        others = [s for s in QGuiApplication.screens() if s is not hud_screen]
        if others:
            self.setGeometry(others[0].geometry())
            self.showFullScreen()
            return f"full screen on monitor {others[0].name()}"
        area = hud_screen.availableGeometry()
        if hud and hud.isVisible():
            g = hud.frameGeometry()
            room_right, room_left = area.right() - g.right(), g.left() - area.left()
            width = max(640, min(1100, max(room_right, room_left) - 16))
            x = g.right() + 8 if room_right >= room_left else max(area.left(), g.left() - width - 8)
            self.setGeometry(x, area.top() + 40, width, min(area.height() - 80, 800))
        else:
            self.setGeometry(area.left() + 80, area.top() + 60, min(1100, area.width() - 160), min(800, area.height() - 120))
        self.showNormal()
        return "window beside the HUD"


def display_mode() -> str:
    """'hud' (in place of the avatar, the default) or 'monitor' (its own window)."""
    try:
        from memory.config_manager import get_plugin_config
        choice = str(get_plugin_config(NAMESPACE).get("display") or "")
    except Exception:
        choice = ""
    return "monitor" if "monitor" in choice.lower() else "hud"


_panel: BrainView | None = None     # inside the HUD
_window: BrainView | None = None    # on its own
_previous_index = 0                 # what the HUD's centre showed before the galaxy took it
_stale = False                      # the vault changed while hidden: reload on the next show


def mark_stale():
    global _stale
    _stale = True


def _stack():
    """The HUD's centre: 0 avatar, 1 camera, 2 video, and the galaxy once it has been opened."""
    hud = _hud()
    return getattr(hud, "_hud_cam_stack", None) if hud else None


def _current() -> BrainView | None:
    """The galaxy that is on screen right now, wherever it is."""
    stack = _stack()
    if _panel is not None and stack is not None and stack.currentWidget() is _panel and _panel.isVisible():
        return _panel
    if _window is not None and _window.isVisible():
        return _window
    return None


# ------------------------------------------------------------------ the API
def _show_on_ui(focus_id=None, light_ids=None):
    global _panel, _window, _previous_index, _stale
    stack = _stack()
    if display_mode() == "hud" and stack is not None:
        if _window is not None:
            _window.hide()
        if _panel is None:
            _panel = BrainView(embedded=True)
            stack.addWidget(_panel)
        elif _stale:
            _panel.reload()
        if stack.currentWidget() is not _panel:
            _previous_index = stack.currentIndex()
            stack.setCurrentWidget(_panel)
        hud = _hud()
        if hud.isMinimized():
            hud.showNormal()
        hud.raise_()
        hud.activateWindow()
        _panel.view.setFocus()
        view, where = _panel, "inside the HUD, in place of the avatar"
    else:
        if _window is None:
            _window = BrainView(embedded=False)
        elif _stale:
            _window.reload()
        where = _window.place()
        _window.raise_()
        _window.activateWindow()
        view = _window
    _stale = False
    if light_ids:
        view.js(f"brain.light({list(light_ids)})")
    elif focus_id is not None:
        view.js(f"setTimeout(() => brain.focus({int(focus_id)}), 900)")
    return {"visible": _current() is view, "where": where}


def show(focus_id=None, light_ids=None) -> dict:
    """Shows the galaxy. Returns {"visible": bool, "where": str} as observed on the GUI thread."""
    vault = get_vault()
    if vault is None:
        return {"visible": False, "where": "", "error": "no vault folder is set"}
    if not HAVE_WEBENGINE:
        import webbrowser
        webbrowser.open(QUrl.fromLocalFile(PAGE).toString())
        return {"visible": True, "where": f"a browser tab ({WEBENGINE_PROBLEM})"}
    state = on_ui(lambda: _show_on_ui(focus_id, light_ids)) or {"visible": False, "where": ""}
    # Notes written in Obsidian since last time get linked too, without anybody asking.
    threading.Thread(target=_auto_link, args=(vault,), name="second-brain-links", daemon=True).start()
    return state


def _auto_link(vault) -> None:
    try:
        from plugins import _second_brain_links as links
        result = links.link_all(vault)
        if result["files_changed"]:
            _log(f"linked {result['linked']} notes by {result['by']} ({result['files_changed']} files updated)")
            reload_open()
    except Exception as e:
        _log(f"automatic linking failed: {e}")


def reload_open() -> None:
    """The vault's links changed: redraw an open galaxy, or redraw it the next time it opens."""
    def job():
        view = _current()
        if view is not None:
            view.reload()
        return view is not None
    if not on_ui(job):
        mark_stale()


def hide() -> bool:
    """Hides the galaxy and gives the HUD its avatar back. True when it is no longer visible."""
    def job():
        stack = _stack()
        if _panel is not None and stack is not None and stack.currentWidget() is _panel:
            ok = 0 <= _previous_index < stack.count() and stack.widget(_previous_index) is not _panel
            stack.setCurrentIndex(_previous_index if ok else 0)
        if _window is not None:
            _window.hide()
        return _current() is None
    return bool(on_ui(job))


def visible() -> bool:
    return bool(on_ui(lambda: _current() is not None, timeout=2))


def toggle() -> None:
    if visible():
        hide()
    else:
        show()


def _js_if_open(code: str) -> bool:
    """Runs code in the galaxy that is on screen. False when none is."""
    def job():
        view = _current()
        if view is not None:
            view.js(code)
        return view is not None
    return bool(on_ui(job))


def focus(node_id: int) -> None:
    _js_if_open(f"brain.focus({int(node_id)})")


def light(ids: list[int]) -> None:
    _js_if_open(f"brain.light({list(map(int, ids))})")


def add_star(node: dict, links: list[dict], near_id) -> None:
    """A new note: a live birth in an open galaxy, a reload the next time otherwise."""
    import json
    public = {k: node[k] for k in ("id", "label", "group", "excerpt", "path")}
    code = f"brain.add({json.dumps(public, ensure_ascii=False)}, {json.dumps(links)}, {json.dumps(near_id)})"
    if not _js_if_open(code):
        mark_stale()


# ------------------------------------------------------------------ Ctrl+Shift+B
def _hotkey_loop():
    """A real global hotkey (works while another app has focus), through RegisterHotKey."""
    user32 = ctypes.windll.user32
    MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT, WM_HOTKEY, VK_B = 0x2, 0x4, 0x4000, 0x0312, 0x42
    if not user32.RegisterHotKey(None, 0xB7A1, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, VK_B):
        _log(f"{HOTKEY_LABEL} is already taken by another program; use the 🧠 button or ask by voice")
        return
    msg = ctypes.wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY:
            threading.Thread(target=toggle, daemon=True).start()


_hotkey_started = False


def start_hotkey():
    global _hotkey_started
    if _hotkey_started or sys.platform != "win32":
        return
    _hotkey_started = True
    import ctypes.wintypes  # noqa: F401  (MSG lives there)
    threading.Thread(target=_hotkey_loop, name="second-brain-hotkey", daemon=True).start()
