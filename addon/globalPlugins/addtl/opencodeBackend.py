# OpenCode backend for agentDesktopAccessibility.
#
# Reads messages directly from OpenCode's SQLite database at
#   %USERPROFILE%\.local\share\opencode\opencode.db
#
# Falls back to virtual-buffer parsing if sqlite3 is unavailable.
#
# The dispatcher in _plugin.py invokes these methods only when OpenCode is the
# foreground app. The original @script decorator and _guard() check lived here
# in the standalone plugin; both moved to the unified dispatcher.

import os
import time
import subprocess
import json

import ui
import api
import winUser
import textInfos
import core
import speech
from logHandler import log
import wx
import gui

_AUTO_READ_INTERVAL_MS = 1000
_CACHE_TTL = 3.0

_DBG_PATH = os.path.join(
    os.path.expanduser("~"), "AppData", "Roaming", "nvda", "opencodeAccessibility_debug.log"
)

_DB_PATH = os.path.join(
    os.path.expanduser("~"), ".local", "share", "opencode", "opencode.db"
)

_DB_CANDIDATES = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "opencode", "data", "opencode.db"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "opencode", "opencode.db"),
    os.path.join(os.environ.get("APPDATA", ""), "opencode", "data", "opencode.db"),
    os.path.join(os.environ.get("APPDATA", ""), "opencode", "opencode.db"),
    os.path.join(os.path.expanduser("~"), ".local", "share", "opencode", "opencode.db"),
    os.environ.get("OPENCODE_DB", ""),
]


def _dbg(*args):
    try:
        with open(_DBG_PATH, "a", encoding="utf-8") as _f:
            _f.write(time.strftime("%H:%M:%S") + "  " + "  ".join(str(a) for a in args) + "\n")
    except Exception:
        pass


class OpenCodeBackend(object):

    def __init__(self, plugin=None):
        # plugin: the parent GlobalPlugin (agentDesktopAccessibility._plugin.GlobalPlugin),
        # used to route auto-read scheduling through its thread context.
        self._plugin = plugin
        self._msgIndex = -1
        self._msgCache = []
        self._msgCacheTime = 0.0
        self._msgCacheSession = ""
        # Auto-read
        self._running = True
        self._autoReadEnabled = True
        self._autoReadSeen = -1
        self._autoReadInitialized = False
        # Session cycle (for NVDA+Alt+Shift+N/P)
        self._sessionsCache = []      # [{label, sid, directory}, ...]
        self._sessionsCacheTs = 0.0
        self._sessionIdx = -1
        self._autoReadSource = None
        self._bufferTextLast = ""
        self._lastSpokenHash = ""
        # Python interpreter for subprocess (cache after first discovery)
        self._pythonExe = None
        try:
            open(_DBG_PATH, "w").close()
        except Exception:
            pass
        _dbg("plugin loaded")
        log.info("OpenCode backend loaded")
        self._scheduleAutoRead()

    def terminate(self):
        self._running = False

    # ------------------------------------------------------------------
    # Python discovery (cached)
    # ------------------------------------------------------------------

    def _getPythonExe(self):
        if self._pythonExe:
            return self._pythonExe
        candidates = [
            "python",
            "python3",
            "py",
            os.path.join(os.environ.get("LOCALAPPDATA", ""),
                         "Programs", "Python", "Python313", "python.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""),
                         "Programs", "Python", "Python312", "python.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""),
                         "Programs", "Python", "Python311", "python.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""),
                         "Programs", "Python", "Python310", "python.exe"),
            os.path.join(os.path.expanduser("~"),
                         "AppData", "Local", "Programs", "Python", "Python313", "python.exe"),
            os.path.join(os.path.expanduser("~"),
                         "AppData", "Local", "Programs", "Python", "Python312", "python.exe"),
            os.path.join(os.path.expanduser("~"),
                         "AppData", "Local", "Programs", "Python", "Python311", "python.exe"),
        ]
        for exe in candidates:
            if not exe:
                continue
            test_cmd = [exe, "-c", "import sqlite3"]
            if exe == "py":
                test_cmd = ["py", "-3", "-c", "import sqlite3"]
            try:
                proc = subprocess.run(
                    test_cmd,
                    capture_output=True, timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                )
                if proc.returncode == 0:
                    self._pythonExe = exe
                    _dbg(f"_getPythonExe: found {exe}")
                    return exe
            except Exception:
                continue
        _dbg("_getPythonExe: no working Python found")
        return None

    # ------------------------------------------------------------------
    # Foreground detection
    # ------------------------------------------------------------------

    def _detectForeground(self):
        out = {
            "hwnd": 0, "title": "", "className": "",
            "accName": "", "appName": "", "productName": "",
            "processPath": "", "pid": 0,
        }
        try:
            hwnd = winUser.getForegroundWindow()
            out["hwnd"] = hwnd
            try:
                out["title"] = winUser.getWindowText(hwnd) or ""
            except Exception:
                pass
            try:
                out["className"] = winUser.getWindowClassName(hwnd) or ""
            except Exception:
                pass
        except Exception:
            return out
        try:
            obj = api.getForegroundObject()
            if obj is not None:
                if not out["title"]:
                    try:
                        out["title"] = obj.name or ""
                    except Exception:
                        pass
                try:
                    out["accName"] = obj.name or ""
                except Exception:
                    pass
                am = getattr(obj, "appModule", None)
                if am is not None:
                    try:
                        out["appName"] = am.appName or ""
                    except Exception:
                        pass
                    try:
                        out["productName"] = am.productName or ""
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            import ctypes
            pid = winUser.getWindowThreadProcessId(hwnd)
            out["pid"] = pid
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                try:
                    buf = ctypes.create_unicode_buffer(512)
                    if psapi.GetModuleFileNameExW(handle, None, buf, 512) > 0:
                        out["processPath"] = buf.value or ""
                finally:
                    kernel32.CloseHandle(handle)
        except Exception:
            pass
        return out

    def _isOpenCode(self):
        info = self._detectForeground()
        for h in [(info.get(k) or "").lower() for k in
                  ("title", "className", "accName", "appName", "productName", "processPath")]:
            if "opencode" in h or "open code" in h or "opencode-desktop" in h:
                return True
        proc = (info.get("processPath") or "").lower()
        if proc:
            base = os.path.basename(proc)
            if "opencode" in base or "open code" in base:
                return True
        return False

    # ------------------------------------------------------------------
    # Tree interceptor helpers (used for fallback + cursor anchoring)
    # ------------------------------------------------------------------

    def _getRawTreeInterceptor(self):
        focus = api.getFocusObject()
        if focus is None:
            return None
        return getattr(focus, "treeInterceptor", None)

    def _getTreeInterceptor(self):
        focus = api.getFocusObject()
        if focus is None:
            return None
        ti = getattr(focus, "treeInterceptor", None)
        if ti is None:
            return None
        if getattr(ti, "passThrough", True):
            return None
        return ti

    # ------------------------------------------------------------------
    # SQLite message parser (subprocess via system Python)
    # ------------------------------------------------------------------

    def _loadMessagesFromDB(self):
        """Read messages from OpenCode's SQLite database via subprocess.

        NVDA's bundled Python lacks sqlite3, so we invoke the system Python
        to run _opencode_db.py which queries the DB and returns JSON.
        """
        db_path = None
        for candidate in _DB_CANDIDATES:
            if candidate and os.path.isfile(candidate):
                db_path = candidate
                break
        if not db_path:
            _dbg("loadDB: db file missing (tried %d candidates)" % len(_DB_CANDIDATES))
            return [], None
        try:
            helper = os.path.join(
                os.path.dirname(__file__), "opencodeDb.py"
            )
            if not os.path.isfile(helper):
                _dbg("loadDB: helper script missing at", helper)
                return [], None
            python_exe = self._getPythonExe()
            if not python_exe:
                _dbg("loadDB: no Python executable found")
                return [], None
            cmd = [python_exe, helper, db_path]
            _dbg("loadDB: running", " ".join(cmd))
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            proc = subprocess.run(
                cmd,
                capture_output=True, encoding='utf-8', timeout=15,
                creationflags=creationflags,
            )
            if proc.returncode != 0:
                _dbg("loadDB: helper exit", proc.returncode)
                _dbg("loadDB: stderr=", proc.stderr[:500])
                _dbg("loadDB: stdout=", proc.stdout[:200])
                return [], None
            data = json.loads(proc.stdout.strip() or "{}")
            msgs = data.get("messages", [])
            sid = data.get("session_id", "")
            _dbg(f"loadDB: session={sid[:20] if sid else 'NONE'} msgs={len(msgs)} db={db_path}")
            for m in msgs[:3]:
                _dbg(f"  [{m['role']}] text={m['text'][:60]!r}")
            return msgs, sid
        except Exception as e:
            _dbg("loadDB error:", e)
            return [], None

    # ------------------------------------------------------------------
    # Virtual-buffer fallback parser
    # ------------------------------------------------------------------

    def _loadMessagesFromBuffer(self):
        """Fallback: return full buffer as a single message when DB is unavailable."""
        ti = self._getRawTreeInterceptor()
        if ti is None:
            return []
        try:
            info = ti.makeTextInfo(textInfos.POSITION_FIRST)
            info.expand(textInfos.UNIT_STORY)
            text = info.text or ""
        except Exception as e:
            _dbg("loadBuffer: getText error:", e)
            return []
        if not text.strip():
            return []
        cleaned = text
        for marker in ("\nuser \u2022 msg_", "\nassistant \u2022 msg_", "user \u2022 msg_", "Raw messages"):
            idx = cleaned.find(marker)
            if idx >= 0:
                cleaned = cleaned[:idx]
                break
        if len(cleaned) > 4000:
            cleaned = cleaned[-4000:]
        cleaned = cleaned.strip()
        if not cleaned:
            return []
        _dbg(f"loadBuffer: 1 message ({len(cleaned)} chars)")
        return [{"role": "OpenCode", "text": cleaned, "thinking": "", "complete": True}]

    # ------------------------------------------------------------------
    # Unified message cache
    # ------------------------------------------------------------------

    def _getMessages(self, force_refresh=False):
        now = time.monotonic()
        stale = (now - self._msgCacheTime) >= _CACHE_TTL

        if not force_refresh and self._msgCache and not stale:
            return self._msgCache, self._autoReadSource

        msgs = []
        sid = ""
        db_messages, sid = self._loadMessagesFromDB()
        source = "db" if sid else "buffer"
        if db_messages:
            msgs = db_messages
        else:
            buffer_messages = self._loadMessagesFromBuffer()
            if buffer_messages:
                msgs = buffer_messages
                source = "buffer"

        if msgs or force_refresh:
            self._msgCache = msgs
            self._msgCacheTime = now
            self._autoReadSource = source
        elif not self._msgCache:
            self._msgCache = []
            self._msgCacheTime = now
            self._autoReadSource = "buffer"

        if sid and sid != self._msgCacheSession:
            self._msgIndex = -1
            self._autoReadInitialized = False
            self._autoReadSeen = -1
            self._autoReadSource = source
            self._bufferTextLast = ""
            self._lastSpokenHash = ""
            self._msgCacheSession = sid
        elif sid:
            self._msgCacheSession = sid

        return self._msgCache, self._autoReadSource

    # ------------------------------------------------------------------
    # Auto-read poller
    # ------------------------------------------------------------------

    def _scheduleAutoRead(self):
        if self._running:
            core.callLater(_AUTO_READ_INTERVAL_MS, self._autoReadCheck)

    def _autoReadCheck(self):
        if not self._running:
            return
        try:
            if not self._autoReadEnabled:
                return
            if not self._isOpenCode():
                return
            msgs, source = self._getMessages()
            assistant_msgs = [m for m in msgs if m["role"] == "Assistant"]
            if assistant_msgs:
                if not self._autoReadInitialized or self._autoReadSource != "db":
                    last_with_text = -1
                    for i, m in enumerate(assistant_msgs):
                        if m["text"]:
                            last_with_text = i
                            self._lastSpokenHash = m["text"]
                    self._autoReadSeen = last_with_text
                    self._autoReadInitialized = True
                    self._autoReadSource = "db"
                    _dbg(f"autoRead init (db): seen idx={self._autoReadSeen} of {len(assistant_msgs)} assistant msgs")
                else:
                    for i in range(self._autoReadSeen + 1, len(assistant_msgs)):
                        m = assistant_msgs[i]
                        text = m["text"]
                        if text and text != self._lastSpokenHash:
                            ui.message("OpenCode: %s" % text)
                            self._lastSpokenHash = text
                            self._autoReadSeen = i
                            _dbg(f"autoRead (db): spoke msg {i}")
                    self._msgIndex = len(msgs) - 1
                self._scheduleAutoRead()
                return
            buffer_msgs = [m for m in msgs if m["role"] == "OpenCode"]
            if buffer_msgs:
                text = buffer_msgs[0]["text"]
                text_len = len(text)
                if not text:
                    self._scheduleAutoRead()
                    return
                if not self._autoReadInitialized or self._autoReadSource != "buffer":
                    self._autoReadSeen = text_len
                    self._autoReadInitialized = True
                    self._autoReadSource = "buffer"
                    self._bufferTextLast = text
                    _dbg(f"autoRead init (buffer): len={text_len}")
                    self._scheduleAutoRead()
                    return
                if text != self._bufferTextLast:
                    if text_len > self._autoReadSeen:
                        new_text = text[self._autoReadSeen:].strip()
                        if new_text:
                            _dbg(f"autoRead (buffer): +{len(new_text)} chars")
                            ui.message(new_text)
                    elif text_len < self._autoReadSeen:
                        _dbg(f"autoRead (buffer): reset, len {self._autoReadSeen} -> {text_len}")
                    self._autoReadSeen = text_len
                    self._bufferTextLast = text
            elif not self._autoReadInitialized:
                text = self._readBufferRaw()
                if text:
                    self._autoReadSeen = len(text)
                    self._autoReadInitialized = True
                    self._autoReadSource = "buffer"
                    self._bufferTextLast = text
                    _dbg(f"autoRead init (direct buffer): len={len(text)}")
        except Exception as e:
            log.warning("opencodeAccessibility: autoRead error: %s", e)
            _dbg("autoRead ERROR:", e)
        self._scheduleAutoRead()

    def _readBufferRaw(self):
        ti = self._getRawTreeInterceptor()
        if ti is None:
            return ""
        try:
            info = ti.makeTextInfo(textInfos.POSITION_FIRST)
            info.expand(textInfos.UNIT_STORY)
            return (info.text or "").strip()
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Cursor anchoring
    # ------------------------------------------------------------------

    def _anchorCursorToText(self, search_text):
        if not search_text:
            return
        ti = self._getRawTreeInterceptor()
        if ti is None:
            return
        needle = search_text[:60].strip()
        if not needle:
            return
        try:
            info = ti.makeTextInfo(textInfos.POSITION_FIRST)
            if info.find(needle, caseSensitive=False):
                info.collapse()
                try:
                    ti.selection = info
                except Exception:
                    pass
        except Exception:
            pass

    def _announceAndAnchor(self, msg):
        speech.cancelSpeech()
        thinking = (msg.get("thinking") or "").strip()
        if thinking:
            ui.message("%s: %s\nThinking: %s" % (msg["role"], msg["text"], thinking))
        else:
            ui.message("%s: %s" % (msg["role"], msg["text"]))
        self._anchorCursorToText(msg["text"])

    # ------------------------------------------------------------------
    # Script guard
    # ------------------------------------------------------------------


    def _findServerPort(self):
        import subprocess
        import socket
        info = self._detectForeground()
        pid = info.get("pid", 0)
        if pid:
            try:
                out = subprocess.run(
                    ["netstat", "-ano"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                ).stdout
                for line in out.splitlines():
                    if f" {pid}" in line or f"\t{pid}" in line or line.rstrip().endswith(f" {pid}"):
                        if "LISTENING" in line:
                            parts = line.split()
                            addr = parts[1] if len(parts) > 1 else ""
                            if ":" in addr:
                                port_str = addr.rsplit(":", 1)[-1]
                                try:
                                    port = int(port_str)
                                    if 1024 <= port <= 65535:
                                        _dbg(f"_findServerPort: netstat found port {port} for pid {pid}")
                                        return port
                                except ValueError:
                                    continue
            except Exception as e:
                _dbg(f"_findServerPort: netstat error {e}")
        for port in range(4096, 4300):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(0.08)
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    sock.close()
                    _dbg(f"_findServerPort: found port {port} via scan")
                    return port
                sock.close()
            except Exception:
                continue
        return None

    def _tryAPINewSession(self):
        port = self._findServerPort()
        if not port:
            _dbg("_tryAPINewSession: no port found")
            return False
        import urllib.request
        urls = [
            (f"http://127.0.0.1:{port}/session", "POST", "{}"),
            (f"http://127.0.0.1:{port}/tui/execute-command",
             "POST", json.dumps({"command": "new"})),
        ]
        for url, method, body in urls:
            try:
                data = body.encode("utf-8")
                req = urllib.request.Request(
                    url, data=data,
                    headers={"Content-Type": "application/json"},
                    method=method,
                )
                resp = urllib.request.urlopen(req, timeout=3)
                _dbg(f"_tryAPINewSession: {method} {url} -> {resp.status}")
                if resp.status < 400:
                    ui.message("New session")
                    return True
            except Exception as e:
                _dbg(f"_tryAPINewSession: {url} error {e}")
                continue
        return False

    # ------------------------------------------------------------------
    # SendInput helpers
    # ------------------------------------------------------------------

    def _sendInputKey(self, vk, scan):
        import ctypes
        user32 = ctypes.windll.user32
        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                        ("dwFlags", ctypes.c_uint), ("time", ctypes.c_uint),
                        ("dwExtraInfo", ctypes.c_void_p)]
        class INPUT(ctypes.Structure):
            _fields_ = [("type", ctypes.c_uint), ("ki", KEYBDINPUT),
                        ("_pad", ctypes.c_ubyte * 8)]
        def _send(vk, scan, up=False):
            inp = INPUT()
            inp.type = 1
            inp.ki.wVk = vk
            inp.ki.wScan = scan
            if up:
                inp.ki.dwFlags = 0x0002
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
        _send(vk, scan, up=False)
        time.sleep(0.02)
        _send(vk, scan, up=True)

    def _sendInputChord(self, mod_vk, mod_scan, key_vk, key_scan):
        import ctypes
        user32 = ctypes.windll.user32
        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                        ("dwFlags", ctypes.c_uint), ("time", ctypes.c_uint),
                        ("dwExtraInfo", ctypes.c_void_p)]
        class INPUT(ctypes.Structure):
            _fields_ = [("type", ctypes.c_uint), ("ki", KEYBDINPUT),
                        ("_pad", ctypes.c_ubyte * 8)]
        def _send(vk, scan, up=False):
            inp = INPUT()
            inp.type = 1
            inp.ki.wVk = vk
            inp.ki.wScan = scan
            if up:
                inp.ki.dwFlags = 0x0002
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
        _send(mod_vk, mod_scan, up=False)
        time.sleep(0.02)
        _send(key_vk, key_scan, up=False)
        time.sleep(0.02)
        _send(key_vk, key_scan, up=True)
        time.sleep(0.02)
        _send(mod_vk, mod_scan, up=True)

    def _tryClipboardNewSession(self):
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        text = "/new"
        old_data = None
        try:
            if user32.OpenClipboard(0):
                try:
                    h = user32.GetClipboardData(CF_UNICODETEXT)
                    if h:
                        size = kernel32.GlobalSize(h)
                        ptr = kernel32.GlobalLock(h)
                        if ptr and size:
                            buf = ctypes.create_string_buffer(size)
                            ctypes.memmove(buf, ptr, size)
                            old_data = buf.raw
                        kernel32.GlobalUnlock(h)
                except Exception:
                    pass
                user32.EmptyClipboard()
                enc = text.encode("utf-16-le") + b"\x00\x00"
                hmem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(enc))
                if hmem:
                    ptr = kernel32.GlobalLock(hmem)
                    ctypes.memmove(ptr, enc, len(enc))
                    kernel32.GlobalUnlock(hmem)
                    user32.SetClipboardData(CF_UNICODETEXT, hmem)
                user32.CloseClipboard()
        except Exception:
            pass
        time.sleep(0.05)
        self._sendInputChord(0x11, 0x1D, 0x56, 0x2F)
        time.sleep(0.1)
        self._sendInputKey(0x0D, 0x1C)
        time.sleep(0.2)
        if old_data is not None:
            try:
                if user32.OpenClipboard(0):
                    user32.EmptyClipboard()
                    hmem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(old_data))
                    if hmem:
                        ptr = kernel32.GlobalLock(hmem)
                        ctypes.memmove(ptr, old_data, len(old_data))
                        kernel32.GlobalUnlock(hmem)
                        user32.SetClipboardData(CF_UNICODETEXT, hmem)
                    user32.CloseClipboard()
            except Exception:
                pass
        ui.message("New session")
        return True

    def _tryCtrlN(self):
        try:
            self._sendInputChord(0x11, 0x1D, 0x4E, 0x31)
            ui.message("New session (Ctrl+N)")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Scripts
    # ------------------------------------------------------------------

    def describeForeground(self):
        info = self._detectForeground()
        msg = "Foreground: %s" % (info["title"] or "(no title)")
        if info["productName"]:
            msg += ", product %s" % info["productName"]
        if info["processPath"]:
            msg += ", %s" % os.path.basename(info["processPath"])
        ti = self._getRawTreeInterceptor()
        msg += ". treeInterceptor: %s" % ("yes" if ti else "no")
        msg += ". guard: %s" % ("PASS" if self._isOpenCode() else "FAIL")
        ui.message(msg)

    def nextMessage(self):
        msgs, _ = self._getMessages()
        if not msgs:
            ui.message("No messages found")
            return
        nxt = self._msgIndex + 1
        if nxt >= len(msgs):
            ui.message("No more messages")
            return
        self._msgIndex = nxt
        self._announceAndAnchor(msgs[self._msgIndex])

    def previousMessage(self):
        msgs, _ = self._getMessages()
        if not msgs:
            ui.message("No messages found")
            return
        nxt = self._msgIndex - 1
        if nxt < 0:
            ui.message("Already at first message")
            return
        self._msgIndex = nxt
        self._announceAndAnchor(msgs[self._msgIndex])

    def firstMessage(self):
        msgs, _ = self._getMessages(force_refresh=True)
        if not msgs:
            ui.message("No messages found")
            return
        self._msgIndex = 0
        self._announceAndAnchor(msgs[0])

    def lastMessage(self):
        msgs, _ = self._getMessages(force_refresh=True)
        if not msgs:
            ui.message("No messages found")
            return
        self._msgIndex = len(msgs) - 1
        self._announceAndAnchor(msgs[-1])

    def readCurrentMessage(self):
        msgs, _ = self._getMessages()
        if not (0 <= self._msgIndex < len(msgs)):
            ui.message("No message selected. Press NVDA+Alt+Down to start.")
            return
        m = msgs[self._msgIndex]
        speech.cancelSpeech()
        ui.message("%s: %s" % (m["role"], m["text"]))

    def readThinking(self):
        msgs, _ = self._getMessages()
        if not (0 <= self._msgIndex < len(msgs)):
            ui.message("No message selected. Press NVDA+Alt+Down to start.")
            return
        m = msgs[self._msgIndex]
        if m["role"] != "Assistant":
            ui.message("Current message is not from the assistant.")
            return
        thinking = (m.get("thinking") or "").strip()
        if thinking:
            ui.message("Thinking: %s" % thinking)
        else:
            ui.message("No thinking available for this message.")

    def toggleAutoRead(self):
        self._autoReadEnabled = not self._autoReadEnabled
        self._autoReadInitialized = False
        state = "on" if self._autoReadEnabled else "off"
        ui.message("OpenCode auto-read %s" % state)

    def dumpDebug(self):
        _dbg("=== DUMP ===")
        info = self._detectForeground()
        _dbg("title=%r class=%r app=%r product=%r path=%r"
             % (info["title"], info["className"], info["appName"],
                info["productName"], info["processPath"]))
        msgs, _ = self._getMessages(force_refresh=True)
        _dbg("messages: %d" % len(msgs))
        for i, m in enumerate(msgs[:12]):
            has_think = "yes" if m.get("thinking") else "no"
            _dbg("  [%d] %s (think=%s): %r" % (i, m["role"], has_think, m["text"][:80]))
        ti = self._getRawTreeInterceptor()
        if ti:
            try:
                info_ti = ti.makeTextInfo(textInfos.POSITION_FIRST)
                info_ti.expand(textInfos.UNIT_STORY)
                raw = info_ti.text[:4000]
                _dbg("--- BUFFER START ---")
                for i in range(0, len(raw), 300):
                    _dbg(repr(raw[i:i + 300]))
                _dbg("--- BUFFER END ---")
            except Exception as e:
                _dbg("buffer dump error:", e)
        else:
            _dbg("no tree interceptor")
        ui.message("Debug log written: %s" % _DBG_PATH)

    def newSession(self):
        if self._tryActivateNewSessionButton():
            return
        if self._tryAPINewSession():
            return
        if self._tryBridgeNewSession():
            return
        if self._tryClipboardNewSession():
            return
        if self._tryCtrlN():
            return
        ui.message("Could not start new session")

    def openSessionPicker(self):
        sessions = self._getOpenCodeSessions()
        if not sessions:
            ui.message("No sessions found")
            return

        # Build display labels: "session title (project name)"
        choices = []
        for s in sessions:
            title = s["label"].split("  \u2014  ")[0].strip()
            directory = s.get("directory", "")
            project = os.path.basename(directory.rstrip("/\\")) if directory else ""
            if project and project != title:
                choices.append("%s  (%s)" % (title, project))
            else:
                choices.append(title)

        def _show():
            gui.mainFrame.prePopup()
            dlg = wx.SingleChoiceDialog(
                gui.mainFrame,
                "Select a session to open",
                "OpenCode Sessions",
                choices,
            )
            result = dlg.ShowModal()
            idx = dlg.GetSelection()
            dlg.Destroy()
            gui.mainFrame.postPopup()
            if result == wx.ID_OK and 0 <= idx < len(sessions):
                picked = sessions[idx]
                directory = picked.get("directory", "")
                session_title = picked["label"].split("  \u2014  ")[0].strip()
                _dbg("script_openSessionPicker: chose %s" % picked["label"])
                if directory and self._tryDeepLinkOpenProject(directory):
                    self._resetSessionState(picked["label"])
                    ui.message("Opening: %s" % session_title)
                else:
                    ui.message("Could not switch to %s" % session_title)

        wx.CallAfter(_show)

    def _tryDeepLinkOpenProject(self, directory):
        try:
            import urllib.parse
            encoded = urllib.parse.quote(directory, safe="")
            url = "opencode://open-project?directory=%s" % encoded
            os.startfile(url)
            _dbg("_tryDeepLinkOpenProject: sent %s" % url[:80])
            return True
        except Exception as e:
            _dbg("_tryDeepLinkOpenProject: error %s" % e)
            return False

    # ------------------------------------------------------------------
    # Session cycle (NVDA+Alt+Shift+N / NVDA+Alt+Shift+P)
    #
    # Wraps _getOpenCodeSessions() with a 30s cache so rapid Shift+N presses
    # don't re-query SQLite on every keypress. The cached list is the source
    # of truth for "where am I" — when the user actually switches, we
    # invalidate the cache so the next cycle picks up the freshest order.
    # ------------------------------------------------------------------

    _SESSIONS_CACHE_TTL = 30.0

    def _refreshSessionsCache(self, force=False):
        now = time.monotonic()
        if (not force
                and self._sessionsCache
                and (now - self._sessionsCacheTs) < self._SESSIONS_CACHE_TTL):
            return
        self._sessionsCache = self._getOpenCodeSessions(max_results=60)
        self._sessionsCacheTs = now
        # If the active session dropped out of the list (e.g. the user
        # archived it externally), reset the index.
        if self._sessionIdx >= len(self._sessionsCache):
            self._sessionIdx = -1
        _dbg("session cache: %d entries" % len(self._sessionsCache))

    def _jumpToSessionIndex(self):
        if not (0 <= self._sessionIdx < len(self._sessionsCache)):
            return
        picked = self._sessionsCache[self._sessionIdx]
        directory = picked.get("directory", "")
        session_title = picked["label"].split("  \u2014  ")[0].strip()
        if directory and self._tryDeepLinkOpenProject(directory):
            self._resetSessionState(picked["label"])
            ui.message("[%d/%d] %s" % (
                self._sessionIdx + 1,
                len(self._sessionsCache),
                session_title))
            _dbg("session cycle: jumped to [%d/%d] %s" % (
                self._sessionIdx + 1, len(self._sessionsCache), session_title))
        else:
            ui.message("Could not switch to %s" % session_title)

    def nextSession(self):
        """Cycle to the next session (NVDA+Alt+Shift+N).

        Order matches _getOpenCodeSessions (most-recently-updated first),
        so the first press jumps to the most recent session the user
        hasn't actively switched to yet. Wraps around at the end."""
        self._refreshSessionsCache()
        if not self._sessionsCache:
            ui.message("No OpenCode sessions found")
            return
        self._sessionIdx = (self._sessionIdx + 1) % len(self._sessionsCache)
        self._jumpToSessionIndex()

    def previousSession(self):
        """Cycle to the previous session (NVDA+Alt+Shift+P).

        Wraps around at the start. Mirrors nextSession in shape so the
        UX is symmetrical — Shift+N forward, Shift+P backward."""
        self._refreshSessionsCache()
        if not self._sessionsCache:
            ui.message("No OpenCode sessions found")
            return
        if self._sessionIdx <= 0:
            self._sessionIdx = len(self._sessionsCache) - 1
        else:
            self._sessionIdx -= 1
        self._jumpToSessionIndex()

    def _bridgeCmdFile(self):
        return os.path.join(
            os.environ.get("TEMP", os.path.expanduser("~")),
            "opencode_nvda_cmd.json"
        )

    def _bridgeRespFile(self):
        return os.path.join(
            os.environ.get("TEMP", os.path.expanduser("~")),
            "opencode_nvda_resp.json"
        )

    def _tryBridgeNewSession(self):
        try:
            import json as _json
            cmd_path = self._bridgeCmdFile()
            resp_path = self._bridgeRespFile()
            if os.path.isfile(resp_path):
                try:
                    os.remove(resp_path)
                except Exception:
                    pass
            with open(cmd_path, "w", encoding="utf-8") as f:
                _json.dump({"action": "new-session"}, f)
            time.sleep(0.6)
            if os.path.isfile(resp_path):
                with open(resp_path, "r", encoding="utf-8") as f:
                    resp = _json.load(f)
                try:
                    os.remove(resp_path)
                except Exception:
                    pass
                if resp.get("ok"):
                    ui.message("New session")
                    _dbg(f"_tryBridgeNewSession: session_id={resp.get('session_id', '')[:20]}")
                    return True
                else:
                    _dbg(f"_tryBridgeNewSession: error={resp.get('error', 'unknown')}")
            else:
                _dbg("_tryBridgeNewSession: no response (plugin not loaded?)")
        except Exception as e:
            _dbg(f"_tryBridgeNewSession: {e}")
        return False

    def _tryActivateNewSessionButton(self):
        ti = self._getRawTreeInterceptor()
        if ti is None:
            _dbg("_tryActivateNewSessionButton: no tree interceptor")
            return False
        try:
            info = ti.makeTextInfo(textInfos.POSITION_FIRST)
            if not info.find("New session", caseSensitive=False):
                _dbg("_tryActivateNewSessionButton: 'New session' not found in buffer")
                return False
            info.collapse()
        except Exception as e:
            _dbg(f"_tryActivateNewSessionButton: find error {e}")
            return False
        try:
            obj = info.NVDAObjectAtPosition
        except Exception:
            obj = None
        if obj is None:
            try:
                info.expand(textInfos.UNIT_CHARACTER)
                obj = info.NVDAObjectAtPosition
            except Exception:
                obj = None
        if obj is not None:
            for _ in range(10):
                try:
                    role = getattr(obj, "role", None)
                    role_str = str(role).lower() if role is not None else ""
                    if any(r in role_str for r in ("button", "link", "menuitem",
                                                     "pushbutton", "togglebutton",
                                                     "listitem", "tab", "graphic")):
                        try:
                            obj.doAction()
                            ui.message("New session")
                            _dbg(f"_tryActivateNewSessionButton: clicked {role_str}")
                            return True
                        except Exception as e:
                            _dbg(f"_tryActivateNewSessionButton: doAction failed: {e}")
                            break
                except Exception:
                    pass
                try:
                    obj = obj.parent
                except Exception:
                    break
                if obj is None:
                    break
        try:
            rects = getattr(info, "boundingRects", None)
            if rects is None:
                try:
                    rects = getattr(info, "_getBoundingRect", None)
                    if rects:
                        rects = rects()
                except Exception:
                    rects = None
            if rects and hasattr(rects, "__iter__") and not isinstance(rects, (str, bytes)):
                rect_list = list(rects)
                if rect_list and len(rect_list[0]) >= 4:
                    r = rect_list[0]
                    cx = int(r[0] + r[2] // 2)
                    cy = int(r[1] + r[3] // 2)
                    _dbg(f"_tryActivateNewSessionButton: click at {cx},{cy}")
                    import ctypes
                    ctypes.windll.user32.SetCursorPos(cx, cy)
                    time.sleep(0.05)
                    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
                    time.sleep(0.03)
                    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
                    time.sleep(0.03)
                    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
                    time.sleep(0.03)
                    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
                    ui.message("New session")
                    return True
        except Exception as e:
            _dbg(f"_tryActivateNewSessionButton: rect/click error {e}")
        _dbg("_tryActivateNewSessionButton: all methods failed")
        return False

    def _resetSessionState(self, label=""):
        self._msgCache = []
        self._msgCacheTime = 0.0
        self._msgCacheSession = ""
        self._msgIndex = -1
        self._autoReadInitialized = False
        self._autoReadSeen = -1
        self._autoReadSource = None
        self._bufferTextLast = ""
        self._lastSpokenHash = ""
        _dbg("_resetSessionState: label=%r" % label)

    def _getOpenCodeSessions(self, max_results=60):
        db_path = None
        for candidate in _DB_CANDIDATES:
            if candidate and os.path.isfile(candidate):
                db_path = candidate
                break
        if not db_path:
            _dbg("_getOpenCodeSessions: no db found")
            return []
        try:
            helper = os.path.join(
                os.path.dirname(__file__), "opencodeDb.py"
            )
            if not os.path.isfile(helper):
                _dbg("_getOpenCodeSessions: helper script missing")
                return []
            python_exe = self._getPythonExe()
            if not python_exe:
                _dbg("_getOpenCodeSessions: no Python")
                return []
            cmd = [python_exe, helper, db_path, "--list"]
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            proc = subprocess.run(
                cmd,
                capture_output=True, encoding="utf-8", timeout=10,
                creationflags=creationflags,
            )
            if proc.returncode != 0:
                _dbg("_getOpenCodeSessions: helper exit", proc.returncode)
                return []
            data = json.loads(proc.stdout.strip() or "{}")
            rows = data.get("sessions", [])
        except Exception as e:
            _dbg("_getOpenCodeSessions: error:", e)
            return []
        sessions = []
        for s in rows:
            sid = s.get("id", "")
            title = (s.get("title") or "").strip()
            directory = (s.get("directory") or "").strip()
            if not sid:
                continue
            label = title if title else directory
            if not label:
                label = sid[:20]
            if directory and directory != title:
                label = "%s  \u2014  %s" % (title or sid[:20], directory)
            sessions.append({"label": label, "sid": sid, "directory": directory})
            if len(sessions) >= max_results:
                break
        _dbg("_getOpenCodeSessions: %d sessions" % len(sessions))
        for s in sessions[:8]:
            _dbg("  %r" % s["label"])
        return sessions
