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
#
# --------------------------------------------------------------------------
# 2.4.1: background poller was destabilizing OpenCode's renderer
# --------------------------------------------------------------------------
# In 2.4.0 the add-on polled OpenCode's SQLite DB every 1s and walked
# the entire accessibility tree on every check. Combined with the
# dispatcher's 1Hz foreground probe, that was enough to push OpenCode's
# `LineDiff` renderer into an infinite-slow path on any session that
# contained a particular kind of code change, hanging the renderer
# window every 10-15 seconds. 2.4.1 backs the poller off:
#   - Auto-read interval: 1s -> 5s
#   - Foreground check:    reuses the router's 250ms cache (no ctypes
#                          walk per tick)
#   - Buffer fallback:     only on explicit hotkey, never from the
#                          poller; long cooldown
#   - ui.message:          queued + 1-per-3s minimum spacing, never
#                          from the per-second tick
# Net effect: the add-on's steady-state cost in OpenCode foreground
# drops from ~30 foreground-queries/min + tree walks to ~12
# foreground-queries/min (cache hits) + no tree walks.

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

# 2.4.1: 1000ms was too aggressive for Electron diff renderers — the
# 1Hz tick combined with the per-tick tree walk was enough to push
# OpenCode's LineDiff into a slow path. 5s is still responsive for
# chat-style auto-read but well below the threshold where the diff
# renderer can wedge.
_AUTO_READ_INTERVAL_MS = 5000
_CACHE_TTL = 5.0

# 2.4.1: minimum spacing between ui.message() calls fired by the
# auto-read poller. 3s prevents the poller from triggering rapid
# layout invalidations in the focused window. Explicit hotkeys
# bypass this throttle (they fire ui.message immediately).
_AUTO_READ_SPEAK_COOLDOWN_S = 3.0

# 2.4.1: the buffer-fallback tree walk (`ti.makeTextInfo(UNIT_STORY)`)
# is the single most expensive thing the add-on does. It must never
# fire from the background poller. The poller uses the SQLite DB
# exclusively; the tree walk is only available on explicit hotkey,
# and even then with a 5s cooldown so that NVDA+Alt+Down spam doesn't
# blast the renderer.
_BUFFER_FALLBACK_COOLDOWN_S = 5.0

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
        # 2.4.1: throttle timestamps for the poller. The poller is no
        # longer allowed to fire ui.message() more than once every
        # _AUTO_READ_SPEAK_COOLDOWN_S, and the buffer-fallback tree
        # walk is forbidden from the poller entirely.
        self._lastSpokeAt = 0.0
        # Session cycle (for NVDA+Alt+Shift+N/P)
        self._sessionsCache = []      # [{label, sid, directory}, ...]
        self._sessionsCacheTs = 0.0
        self._sessionIdx = -1
        self._autoReadSource = None
        self._bufferTextLast = ""
        self._lastSpokenHash = ""
        # 2.4.1: separate cooldown for the tree-walk fallback. Only
        # reset on explicit hotkey; the poller never reads this.
        self._lastBufferFallbackAt = 0.0
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
    #
    # 2.4.1: the heavy ctypes-based detector (processPath via
    # GetModuleFileNameExW + accessibility tree walk) is preserved for
    # explicit hotkey paths where we *need* to be 100% sure of the
    # foreground. The poller uses the router's cached version instead,
    # which only re-checks every 250ms and never does ctypes.
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
        # 2.4.1: the poller used to call this on every tick, which did
        # a ctypes walk + accessibility tree query every 1s. The
        # router's cached check (250ms TTL, no ctypes) is now the
        # preferred path for the poller; this method is still here
        # for explicit hotkey paths that need full reliability.
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

    def _isOpenCodeCached(self):
        """Cheap foreground check for the poller (2.4.1).

        Uses the router's 250ms cache so the poller doesn't trigger
        its own ctypes walk every tick. The router's _refresh() walks
        the appModule tree but does NOT do ctypes — it's a few orders
        of magnitude cheaper than _isOpenCode().

        Returns True only if the router is reasonably confident the
        foreground is OpenCode. If the router cache is stale or the
        router's heuristic missed (e.g. an unusual title), the poller
        will skip this tick — that's the safe direction.
        """
        try:
            from .router import is_opencode
            return bool(is_opencode())
        except Exception:
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

    def _getMessages(self, force_refresh=False, allow_buffer_fallback=True):
        # 2.4.1: `allow_buffer_fallback` is False when called from the
        # background poller. The buffer fallback does a full
        # `ti.makeTextInfo(UNIT_STORY)` walk of OpenCode's accessibility
        # tree, which forces the Electron renderer to re-render every
        # visible CollapsibleRoot (including every code diff). Doing
        # that on a 1Hz tick is what was wedging OpenCode's LineDiff
        # renderer. The poller must only ever read the SQLite DB;
        # buffer fallback is reserved for explicit hotkey paths, which
        # are user-initiated one-shots.
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
        elif allow_buffer_fallback:
            # 2.4.1: gated on the cooldown so explicit hotkey paths
            # don't blast the renderer either. Hotkey paths reset
            # the cooldown before calling.
            if (now - self._lastBufferFallbackAt) >= _BUFFER_FALLBACK_COOLDOWN_S:
                self._lastBufferFallbackAt = now
                buffer_messages = self._loadMessagesFromBuffer()
                if buffer_messages:
                    msgs = buffer_messages
                    source = "buffer"
            else:
                _dbg("_getMessages: buffer fallback on cooldown, skipping")

        if msgs or force_refresh:
            self._msgCache = msgs
            self._msgCacheTime = now
            self._autoReadSource = source
        elif not self._msgCache:
            self._msgCache = []
            self._msgCacheTime = now
            self._autoReadSource = "db" if not allow_buffer_fallback else "buffer"

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
        # 2.4.1: this poller used to do a full ctypes foreground walk
        # + SQLite read + accessibility-tree walk on every 1s tick. The
        # combination was enough to push OpenCode's LineDiff renderer
        # into a slow path that hung the window. Now:
        #   - foreground check uses the router's 250ms cache
        #   - message source is the SQLite DB only
        #   - the buffer/tree fallback is skipped entirely from the
        #     poller (gated behind _BUFFER_FALLBACK_COOLDOWN_S)
        #   - ui.message() calls are throttled to 1 per 3s
        if not self._running:
            return
        try:
            if not (self._autoReadEnabled and self._isOpenCodeCached()):
                return
            msgs, source = self._getMessages(allow_buffer_fallback=False)
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
                    now = time.monotonic()
                    for i in range(self._autoReadSeen + 1, len(assistant_msgs)):
                        m = assistant_msgs[i]
                        text = m["text"]
                        if text and text != self._lastSpokenHash:
                            # 2.4.1: throttle. The poller used to fire
                            # ui.message on every new text byte; combined
                            # with the per-tick tree refresh, that was
                            # enough to make Electron's renderer re-layout
                            # so often that its diff code couldn't finish.
                            if (now - self._lastSpokeAt) >= _AUTO_READ_SPEAK_COOLDOWN_S:
                                ui.message("OpenCode: %s" % text)
                                self._lastSpokeAt = now
                                self._lastSpokenHash = text
                                self._autoReadSeen = i
                                _dbg(f"autoRead (db): spoke msg {i}")
                            else:
                                _dbg(f"autoRead (db): msg {i} skipped (speak cooldown)")
                    self._msgIndex = len(msgs) - 1
            # 2.4.1: the buffer-source auto-read path (and the
            # direct-buffer fallback inside _autoReadCheck) is removed.
            # The poller no longer reads the accessibility tree — that's
            # the single most expensive thing the add-on can do, and
            # doing it from a 1Hz loop is what was destabilizing the
            # renderer. If the user wants the buffer-source view, they
            # can press NVDA+Alt+Down (next message) or NVDA+Alt+R
            # (re-read) which use the explicit-hotkey path with the
            # cooldown.
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
                session_id = picked.get("sid", "")
                _dbg("script_openSessionPicker: chose %s" % picked["label"])
                if self._openProjectAndFocusSession(directory, session_id, session_title):
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

    def _tryDeepLinkOpenSession(self, directory, session_id):
        """Open a specific session via the opencode://session deep link.

        NOTE (2.4.0): This deep link is NOT registered in OpenCode's
        renderer — it is silently dropped by `parseDeepLink`, which only
        recognizes `open-project` and `new-session`. The earlier
        `patch_opencode_asar.js` patcher that used to add a `session`
        hostname handler to the renderer was removed in 2.4.0 because
        patching OpenCode's app.asar was found to destabilize the app.
        This method is kept for source compatibility (and so future
        upstream support can be detected by the renderer accepting the
        URL without complaint) but currently behaves the same as
        `_tryDeepLinkOpenProject` — i.e. it lands on the most-recent
        session in the project, not the picked one.
        """
        # Fire the URL so the OS hands it to the renderer; whether the
        # renderer actually does anything with it is up to OpenCode.
        try:
            import urllib.parse
            enc_dir = urllib.parse.quote(directory, safe="")
            enc_id = urllib.parse.quote(session_id, safe="")
            url = "opencode://session?id=%s&directory=%s" % (enc_id, enc_dir)
            os.startfile(url)
            _dbg("_tryDeepLinkOpenSession: sent %s" % url[:100])
            return True
        except Exception as e:
            _dbg("_tryDeepLinkOpenSession: error %s" % e)
            return False

    # ------------------------------------------------------------------
    # Open project + focus a specific session within it
    #
    # When the user picks a session from the picker (or cycles to one
    # with NVDA+Alt+Shift+N/P), we want to land on a session in the
    # right project.
    #
    # History:
    #   2.2.0 — only the `opencode://open-project?directory=...` link
    #           was available; the user landed on whatever the project
    #           auto-opened to (most recent session).
    #   2.3.0 — added an `opencode://session?id=<id>&directory=<dir>`
    #           link and a JS patcher (patch_opencode_asar.js) that
    #           added a corresponding hostname handler to the renderer,
    #           so the picked session was opened exactly.
    #   2.4.0 — the JS patcher was removed because it destabilized
    #           OpenCode's renderer (it was making changes to a file
    #           that OpenCode's own update flow regularly overwrites,
    #           and the self-heal retry was firing too aggressively
    #           in some setups). Without the patch, the session deep
    #           link is silently dropped by the renderer, so we
    #           fall back to the project-only deep link. The user
    #           still lands in the correct project — just not
    #           necessarily on the exact session they picked.
    # ------------------------------------------------------------------

    def _openProjectAndFocusSession(self, directory, session_id, session_title):
        """Open a project and (if possible) focus a specific session.

        Returns True on success (deep link sent).
        Returns False if the project couldn't be opened at all.
        """
        if not directory:
            return False
        # Try the dedicated session deep link first. In OpenCode builds
        # that don't recognize `opencode://session`, the renderer
        # silently drops it and the project-only link below takes over.
        if session_id and self._tryDeepLinkOpenSession(directory, session_id):
            self._resetSessionState(session_title or "")
            return True
        # Fall back to the project-only deep link. This is the path
        # that always works without any asar patching.
        if self._tryDeepLinkOpenProject(directory):
            self._resetSessionState(session_title or "")
            return True
        _dbg("_openProjectAndFocusSession: all deep links failed for %s" % directory)
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
        session_id = picked.get("sid", "")
        if self._openProjectAndFocusSession(directory, session_id, session_title):
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
