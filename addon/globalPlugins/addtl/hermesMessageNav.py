# -*- coding: UTF-8 -*-
# globalPlugins/hermesMessageNav.py
#
# Chat message navigation + session browsing for Hermes Agent.
# Queries Hermes' state.db via system Python subprocess
# (NVDA's embedded Python lacks sqlite3).
#
# Gestures:
#   NVDA+Alt+N       — Next message
#   NVDA+Alt+P       — Previous message
#   NVDA+Alt+L       — Last (newest) message
#   NVDA+Alt+C       — Re-read current
#   NVDA+Alt+R       — Refresh now (force fresh fetch)
#   NVDA+Alt+S       — Session picker dialog
#   NVDA+Alt+Shift+N — Quick next session (no dialog)
#   NVDA+Alt+Shift+P — Quick previous session
#   NVDA+Alt+I       — Position info + context
#   NVDA+Alt+D       — Full diagnostic dump

import globalPluginHandler
import api
import ui
import speech
import os
import sys
import time
import subprocess
import json

try:
    import wx
except ImportError:
    wx = None

# ── Paths ───────────────────────────────────────────────────────────

def _get_hermes_db():
    hh = os.environ.get('HERMES_HOME', '')
    if not hh:
        la = os.environ.get('LOCALAPPDATA',
                            os.path.expandvars(r'%USERPROFILE%\AppData\Local'))
        hh = os.path.join(la, 'hermes')
    return os.path.join(hh, 'state.db')

_HERMES_DB = _get_hermes_db()


# ── System Python finder ────────────────────────────────────────────

def _find_system_python():
    import shutil
    candidates = []
    py = shutil.which('python')
    if py:
        candidates.append(py)
    for ver in ['313', '312', '311', '310']:
        for base in [
            r'C:\Python' + ver + r'\python.exe',
            r'C:\Program Files\Python' + ver + r'\python.exe',
            os.path.expandvars(
                r'%LOCALAPPDATA%\Programs\Python\Python' + ver + r'\python.exe'),
        ]:
            if os.path.exists(base):
                candidates.append(base)
    py_l = shutil.which('py')
    if py_l:
        candidates.append(py_l)
    cf = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
    for exe in candidates:
        try:
            r = subprocess.run(
                [exe, '-c', 'import sqlite3; print("OK")'],
                capture_output=True, text=True, timeout=8, creationflags=cf)
            if r.returncode == 0 and 'OK' in r.stdout:
                return exe
        except Exception:
            continue
    return None

_PYTHON_EXE = None

def _python():
    global _PYTHON_EXE
    if _PYTHON_EXE is None:
        _PYTHON_EXE = _find_system_python()
    return _PYTHON_EXE


def _run_script(script, *args, retries=3):
    """Run a Python script via subprocess; retry on transient errors."""
    py = _python()
    if py is None:
        return {"error": "no system Python found"}
    cf = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
    last_err = None
    for attempt in range(retries):
        try:
            r = subprocess.run(
                [py, '-c', script, *args],
                capture_output=True, text=True, timeout=15, creationflags=cf)
            if r.returncode == 0 and r.stdout.strip():
                return json.loads(r.stdout)
            stderr = r.stderr[:300]
            # SQLite lock errors — retry
            if 'database is locked' in stderr.lower():
                last_err = "db locked (attempt %d)" % (attempt + 1)
                time.sleep(0.3 * (attempt + 1))
                continue
            return {"error": "exit=%d: %s" % (r.returncode, stderr)}
        except subprocess.TimeoutExpired:
            last_err = "timeout (attempt %d)" % (attempt + 1)
            time.sleep(0.5)
            continue
        except Exception as e:
            return {"error": str(e)}
    return {"error": last_err or "unknown"}


# ── Query scripts ───────────────────────────────────────────────────

_SESSION_LIST_SCRIPT = r'''
import sqlite3, json, sys
db = sys.argv[1]
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
cur = con.cursor()
rows = cur.execute("""
    SELECT id, title, started_at, message_count, source,
           (SELECT substr(content,1,120) FROM messages
            WHERE session_id=s.id AND role='user' AND active=1
            ORDER BY id LIMIT 1) as first_msg
    FROM sessions s
    WHERE source IN ('tui','desktop') AND message_count>0
    ORDER BY started_at DESC LIMIT 20
""").fetchall()
con.close()
sessions = []
for r in rows:
    t = r['title'] or ''
    if not t:
        fm = r['first_msg'] or ''
        t = fm[:50] if fm else '(untitled)'
    sessions.append({
        'id': r['id'], 'title': t,
        'started_at': r['started_at'],
        'message_count': r['message_count'],
        'source': r['source'],
    })
print(json.dumps({'sessions': sessions}))
'''

_MESSAGES_SCRIPT = r'''
import sqlite3, json, sys, os
db = sys.argv[1]
sid = sys.argv[2]
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
cur = con.cursor()
rows = cur.execute("""
    SELECT role, content, reasoning_content FROM messages
    WHERE session_id=? AND role IN ('user','assistant') AND active=1
    ORDER BY id
""", (sid,)).fetchall()
# Check if last message is from user (Hermes is still processing)
last_role = rows[-1]['role'] if rows else None
meta = cur.execute(
    "SELECT title, started_at, message_count FROM sessions WHERE id=?", (sid,)
).fetchone()
con.close()
msgs = []
for r in rows:
    c = (r['content'] or '').strip()
    rc = (r['reasoning_content'] or '').strip()
    t = c if c else ('[thinking] ' + rc if rc else '')
    if not t or len(t) < 3:
        continue
    if t.startswith('{') and t.endswith('}') and len(t) < 200:
        try:
            json.loads(t); continue
        except Exception:
            pass
    msgs.append(t)

# If last DB message is from user, check for streaming temp file
streaming_text = ""
if last_role == 'user':
    hermes_home = os.environ.get('HERMES_HOME', '')
    if not hermes_home:
        la = os.environ.get('LOCALAPPDATA',
            os.path.expandvars(r'%USERPROFILE%\\AppData\\Local'))
        hermes_home = os.path.join(la, 'hermes')
    streaming_path = os.path.join(hermes_home, f'streaming_{sid}.txt')
    try:
        if os.path.exists(streaming_path):
            with open(streaming_path, 'r', encoding='utf-8') as f:
                streaming_text = f.read().strip()
            if streaming_text:
                msgs.append(streaming_text)
    except Exception:
        pass

print(json.dumps({
    'messages': msgs,
    'title': meta['title'] if meta and meta['title'] else '',
    'started_at': meta['started_at'] if meta else 0,
    'msg_count': meta['message_count'] if meta else 0,
    'streaming': bool(streaming_text)
}))
'''

# Quick script to detect the session with the most recent message activity.
# Runs standalone so it's fast (< 100ms) and doesn't need to find the
# session by index — just returns the most-recently-active session ID.
_CURRENT_SESSION_SCRIPT = r'''
import sqlite3, json, sys
db = sys.argv[1]
con = sqlite3.connect(db)
cur = con.cursor()
row = cur.execute("""
    SELECT s.id, s.title FROM sessions s
    WHERE EXISTS (SELECT 1 FROM messages m WHERE m.session_id=s.id)
      AND s.source IN ('tui','desktop')
    ORDER BY (SELECT MAX(m.id) FROM messages m WHERE m.session_id=s.id) DESC
    LIMIT 1
""").fetchone()
con.close()
if row:
    print(json.dumps({'id': row[0], 'title': row[1] or ''}))
else:
    print(json.dumps({'id': None, 'title': ''}))
'''


# ── Formatting ──────────────────────────────────────────────────────

def _fmt_time(ts):
    try:
        t = time.localtime(ts)
        now = time.localtime()
        if t.tm_year == now.tm_year and t.tm_yday == now.tm_yday:
            return time.strftime('today %I:%M %p', t).lower().lstrip('0')
        elif t.tm_year == now.tm_year and t.tm_yday == now.tm_yday - 1:
            return time.strftime('yest %I:%M %p', t).lower().lstrip('0')
        else:
            return time.strftime('%b %d %I:%M %p', t).lower().lstrip('0')
    except Exception:
        return '?'

def _fmt_session(s, idx, current=False):
    marker = ' *' if current else '  '
    return "%s%s (%d msgs)  %s" % (
        marker, s.get('title', '?')[:55],
        s.get('message_count', 0), _fmt_time(s.get('started_at', 0)))


def _isHermesFg():
    """Check if Hermes is the foreground app (informational)."""
    try:
        fg = api.getForegroundObject()
        if fg is not None:
            name = getattr(getattr(fg, 'appModule', None), 'appName', '') or ''
            return 'hermes' in name.lower()
    except Exception:
        pass
    return False


# ═══════════════════════════════════════════════════════════════════
#  Session + message manager
# ═══════════════════════════════════════════════════════════════════

class _SessionManager:
    def __init__(self):
        self._sessions = []
        self._current_sid = None
        self._current_sidx = -1
        self._messages = []
        self._msg_index = -1
        self._sessions_ts = 0
        self._messages_ts = 0
        self._last_msg_count = 0
        self._streaming_active = False
        self._error = None

    # ── Session list ────────────────────────────────────────────

    def refresh_sessions(self):
        now = time.time()
        if now - self._sessions_ts < 5.0 and self._sessions:
            return
        self._sessions_ts = now
        result = _run_script(_SESSION_LIST_SCRIPT, _HERMES_DB)
        if result.get('error'):
            self._error = result['error']
            return
        self._sessions = result.get('sessions', [])
        self._error = None
        # Re-sync current session index
        if self._current_sid:
            for i, s in enumerate(self._sessions):
                if s['id'] == self._current_sid:
                    self._current_sidx = i
                    break
            else:
                self._current_sid = None
                self._current_sidx = -1
        if self._current_sid is None and self._sessions:
            self._select_index(0, announce=False)

    def _select_index(self, index, announce=True, load_messages=True):
        """Select session at index. DOES NOT clear messages until
        new ones successfully load.

        load_messages=False skips the subprocess message query — use
        for quick session switching where only the session metadata
        (title, message_count) is needed from the cached session list.
        Messages will be loaded lazily on first navigation."""
        if not self._sessions or index < 0 or index >= len(self._sessions):
            return False
        self._current_sidx = index
        self._current_sid = self._sessions[index]['id']
        self._msg_index = -1
        self._last_msg_count = 0
        self._streaming_active = False
        self._messages_ts = 0
        if load_messages:
            # Load new messages — but keep old ones until success
            ok = self._do_refresh_messages()
            if not ok:
                # Keep whatever messages we had (or empty if first load)
                pass
        return True

    def select_index(self, index):
        """Public: select session by index."""
        if index < 0 or index >= len(self._sessions):
            ui.message("Invalid session index %d" % index)
            return False
        self._select_index(index)
        s = self._sessions[index]
        if self.msg_count > 0:
            ui.message("%s — %d messages loaded" % (
                s['title'][:60], self.msg_count))
        elif self._error:
            ui.message("Error loading messages: %s" % self._error)
        else:
            ui.message("Loaded %s (no messages yet)" % s['title'][:60])
        return True

    def select_next(self):
        self.refresh_sessions()
        if not self._sessions:
            return False
        idx = self._current_sidx + 1
        if idx >= len(self._sessions):
            idx = 0
        return self._select_index(idx, load_messages=False)

    def select_prev(self):
        self.refresh_sessions()
        if not self._sessions:
            return False
        idx = self._current_sidx - 1
        if idx < 0:
            idx = len(self._sessions) - 1
        return self._select_index(idx, load_messages=False)

    # ── Messages ────────────────────────────────────────────────

    def _detect_current_session(self):
        """Detect the most-recently-active session from state.db.
        If it differs from _current_sid, auto-switch to it.
        Returns True if we switched, False otherwise."""
        result = _run_script(_CURRENT_SESSION_SCRIPT, _HERMES_DB)
        if result.get('error'):
            return False
        new_id = result.get('id')
        if not new_id or new_id == self._current_sid:
            return False
        # Find this session in our list
        for i, s in enumerate(self._sessions):
            if s['id'] == new_id:
                self._current_sidx = i
                self._current_sid = new_id
                self._msg_index = -1
                self._last_msg_count = 0
                self._messages_ts = 0
                # Load messages from the new session
                self._do_refresh_messages()
                return True
        # Session not in our list — refresh and try again
        self.refresh_sessions()
        for i, s in enumerate(self._sessions):
            if s['id'] == new_id:
                self._current_sidx = i
                self._current_sid = new_id
                self._msg_index = -1
                self._last_msg_count = 0
                self._messages_ts = 0
                self._do_refresh_messages()
                return True
        return False

    def _do_refresh_messages(self):
        """Query messages. Returns True on success, False on error.
        On success, replaces self._messages. On error, keeps old.

        Always replaces self._messages with the new list when the call
        succeeds. We avoid premature short-circuits because the streaming
        temp file can change on every fetch (even when the DB count is
        stable), and because the final assistant message replaces the
        streaming chunk in place when the turn ends — same length, but
        the content differs and the old code would refuse to update.
        """
        if self._current_sid is None:
            return False
        result = _run_script(_MESSAGES_SCRIPT, _HERMES_DB, self._current_sid)
        if result.get('error'):
            self._error = result['error']
            return False
        mc = result.get('msg_count', 0)
        streaming = result.get('streaming', False)
        new_msgs = result.get('messages', [])
        new_total = len(new_msgs)
        # Decide whether to replace self._messages:
        # - DB count changed → always update
        # - streaming flag set (temp file present) → always update, since
        #   the streaming content can change on every fetch
        # - list length changed (e.g. streaming chunk replaced by final
        #   assistant message — same length, but content differs) → update
        # - content actually differs from current → update
        # Otherwise skip the assignment to avoid no-op churn.
        db_count_changed = mc != self._last_msg_count
        length_changed = new_total != self.msg_count
        content_changed = (
            new_total != len(self._messages)
            or any(
                (new_msgs[i] if i < new_total else None)
                != (self._messages[i] if i < len(self._messages) else None)
                for i in range(max(new_total, len(self._messages)))
            )
        )
        if db_count_changed or streaming or length_changed or content_changed:
            self._messages = new_msgs
            self._last_msg_count = mc
            self._error = None
        # Update title in session list
        if 0 <= self._current_sidx < len(self._sessions):
            t = result.get('title', '')
            if t:
                self._sessions[self._current_sidx]['title'] = t
        return True

    def refresh_messages(self, force=False):
        """Throttled message refresh. Keeps existing messages on error.
        Uses a shorter throttle when streaming content was detected.

        force=True bypasses the throttle and always runs a fresh fetch.
        Hotkey handlers pass force=True so every press reads the latest
        data; the auto-refresh timer passes force=False so it doesn't
        hammer the DB."""
        if self._current_sid is None:
            return
        now = time.time()
        throttle = 0.25 if self._streaming_active else 0.5
        if not force and now - self._messages_ts < throttle:
            return
        self._messages_ts = now
        self._do_refresh_messages()
        # Track streaming state for dynamic throttle. Streaming is
        # active when the assistant's text is in the temp file (DB only
        # has the user message) — i.e. _last_msg_count < msg_count and
        # the last message is a streaming chunk.
        last = self._messages[-1] if self._messages else ""
        is_streaming_chunk = (
            last and not last.startswith(("{", "["))
            and self._last_msg_count < len(self._messages)
        )
        self._streaming_active = bool(is_streaming_chunk)

    # ── Accessors ───────────────────────────────────────────────

    @property
    def sessions(self):
        return self._sessions
    @property
    def current_session(self):
        if 0 <= self._current_sidx < len(self._sessions):
            return self._sessions[self._current_sidx]
        return None
    @property
    def session_count(self):
        return len(self._sessions)
    @property
    def session_index(self):
        return self._current_sidx
    @property
    def messages(self):
        return self._messages
    @property
    def msg_count(self):
        return len(self._messages)
    def get_message(self, i):
        if 0 <= i < len(self._messages):
            return self._messages[i]
        return None
    @property
    def msg_index(self):
        return self._msg_index
    @msg_index.setter
    def msg_index(self, val):
        self._msg_index = val


_mgr = _SessionManager()


# ═══════════════════════════════════════════════════════════════════
#  Session picker dialog
# ── Session picker dialog ──────────────────────────────────────────

class _SessionPickerDialog(wx.Dialog):
    """A session selection dialog with proper Enter/Escape handling."""

    def __init__(self, parent, sessions, current_index):
        super().__init__(
            parent, title="Hermes Sessions",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._sessions = sessions
        self._selected = -1

        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        label = wx.StaticText(panel, label="Select a session (%d total):" % len(sessions))
        sizer.Add(label, 0, wx.ALL, 10)

        choices = []
        for i, s in enumerate(sessions):
            choices.append(_fmt_session(s, i, i == current_index))

        self._list = wx.ListBox(panel, choices=choices, style=wx.LB_SINGLE)
        if 0 <= current_index < len(sessions):
            self._list.SetSelection(current_index)
        sizer.Add(self._list, 1, wx.EXPAND | wx.ALL, 10)

        btn_sizer = wx.StdDialogButtonSizer()
        ok_btn = wx.Button(panel, wx.ID_OK, "OK")
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        btn_sizer.AddButton(ok_btn)
        btn_sizer.AddButton(cancel_btn)
        btn_sizer.Realize()
        sizer.Add(btn_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        panel.SetSizer(sizer)
        self.SetInitialSize(wx.Size(650, 420))
        self.Centre()

        # Ensure OK is the default button so Enter fires it from any control
        ok_btn.SetDefault()
        # Bind Enter on the list directly (EVT_CHAR_HOOK catches it even if
        # the list internally swallows EVT_KEY_DOWN)
        self._list.Bind(wx.EVT_CHAR_HOOK, self._onListKey)
        # Double-click on list = OK
        self._list.Bind(wx.EVT_LISTBOX_DCLICK, lambda e: self.EndModal(wx.ID_OK))

    def _onListKey(self, event):
        key = event.GetKeyCode()
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self.EndModal(wx.ID_OK)
        else:
            event.Skip()

    def GetSelection(self):
        return self._list.GetSelection()


def _doShowPicker():
    """Show the session picker dialog. Called via wx.CallAfter."""
    import gui

    _mgr.refresh_sessions()
    sessions = _mgr.sessions
    if not sessions:
        ui.message("No Hermes sessions found")
        return

    selected_sid = None
    gui.mainFrame.prePopup()
    try:
        dlg = _SessionPickerDialog(
            gui.mainFrame, sessions, _mgr.session_index
        )
        if dlg.ShowModal() == wx.ID_OK:
            idx = dlg.GetSelection()
            if idx >= 0 and idx < len(sessions):
                selected_sid = sessions[idx]['id']
                _mgr.select_index(idx)
        dlg.Destroy()
    finally:
        gui.mainFrame.postPopup()

    # Switch session AFTER dialog is fully closed and focus is restored
    if selected_sid:
        _resumeHermesSession(selected_sid, delay=0.25)


# ── App.asar auto-patch ─────────────────────────────────────────────
# Ensures the Hermes desktop app supports hermes://session/<id> deep links.
#
# The bundled Hermes app's handleDeepLink() only forwards `kind=blueprint`
# deep links to the renderer. The patch script (patch_app_asar.js) injects
# a 3-line `if (kind === 'session' && name)` branch into handleDeepLink that
# routes session deep links to the existing `hermes:focus-session` IPC channel
# — which the renderer's onFocusSession listener already handles by calling
# sessionRoute(sessionId).
#
# Self-healing contract (v2.1.0):
#   - Re-check on every session-pick (NOT once per NVDA session). The previous
#     design cached `_patch_checked = True` even when the patch failed, which
#     left the user with a "looks like it works" addon that silently dropped
#     session deep links after every Hermes update.
#   - On failure, *announce* it to the user (ui.message) so they know the
#     picker will misbehave, instead of pretending everything is fine.
#   - Throttle: still skip re-checking if the last successful check was
#     within the last 60 seconds. Hermes doesn't write to app.asar on its
#     own; the only writers are the update flow and the patcher itself.
#   - Idempotent: patch_app_asar.js is safe to re-run; if already patched,
#     it exits with PATCHED in <1s.
#
# To make this truly permanent, the upstream Hermes handleDeepLink() should
# route `kind=session` natively. The PR is prepared in the addon's repo;
# see hermesDesktopAccessibility README for details.

_PATCH_MARKER = "kind === 'session' && name"  # MUST match patch_app_asar.js
_PATCH_TTL_S = 60.0   # how long a successful check is considered fresh
_patch_state = {      # last known patch status
    'last_check_t': 0.0,
    'patched': None,  # None=unknown, True=patched, False=not patched
    'last_error': None,
}


def _find_patch_script():
    """Return the absolute path to patch_app_asar.js, or None."""
    # __file__ is .../addon/globalPlugins/hermesMessageNav.py
    # Addon root is .../addon/ (two levels up from this file)
    addon_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(addon_root, 'patch_app_asar.js'),
        os.path.join(os.path.expanduser('~'), 'programs',
                     'nvda agent desktop accessibility', 'patch_app_asar.js'),
    ]
    for p in candidates:
        p = os.path.normpath(p)
        if os.path.exists(p):
            return p
    return None


def _find_node_exe():
    """Return the absolute path to node.exe, or None."""
    import shutil
    node = shutil.which('node')
    if node:
        return node
    for np in [
        os.path.join(os.environ.get('PROGRAMFILES', ''), 'nodejs', 'node.exe'),
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'nodejs', 'node.exe'),
    ]:
        if os.path.exists(np):
            return np
    return None


def _ensureAppAsarPatched(verbose=False):
    """Ensure the Hermes app.asar has the session deep-link patch.

    Self-healing: re-checks every call (subject to a 60s TTL cache), and
    *reports* failure rather than silently proceeding.

    Returns True iff the patch is in place (either pre-existing or just
    applied). Returns False if the patch is missing AND we couldn't apply
    it. Side effect: may show a ui.message on failure so the user knows
    the session picker is broken.
    """
    import time
    now = time.monotonic()
    if (_patch_state['patched'] is True
            and (now - _patch_state['last_check_t']) < _PATCH_TTL_S):
        return True

    script = _find_patch_script()
    if not script:
        # No patch script available — assume already patched, or give up.
        # We can't fix it; don't keep nagging.
        _patch_state.update({'last_check_t': now, 'patched': True, 'last_error': 'no patch script'})
        return True

    node = _find_node_exe()
    if not node:
        _patch_state.update({'last_check_t': now, 'patched': True, 'last_error': 'no node.exe'})
        return True

    cf = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
    try:
        # 1. Quick check
        r = subprocess.run(
            [node, script, '--check'],
            capture_output=True, text=True, timeout=15, creationflags=cf)
        if r.returncode == 0 and 'PATCHED' in r.stdout:
            _patch_state.update({'last_check_t': now, 'patched': True, 'last_error': None})
            return True
        if verbose:
            log.info("hermes asar patch missing — applying")
        # 2. Apply
        r2 = subprocess.run(
            [node, script],
            capture_output=True, text=True, timeout=60, creationflags=cf)
        if r2.returncode == 0 and 'PATCHED_SUCCESS' in r2.stdout:
            _patch_state.update({'last_check_t': now, 'patched': True, 'last_error': None})
            return True
        # 3. Patch failed
        err = (r2.stdout + ' ' + r2.stderr).strip() or ('exit=%d' % r2.returncode)
        _patch_state.update({'last_check_t': now, 'patched': False, 'last_error': err})
        log.warning("hermes asar patch FAILED: %s", err)
        try:
            ui.message("Hermes session patch failed: %s. Session picker will not work." % err[:120])
        except Exception:
            pass
        return False
    except subprocess.TimeoutExpired:
        _patch_state.update({'last_check_t': now, 'patched': False, 'last_error': 'timeout'})
        try:
            ui.message("Hermes session patch timed out. Try again.")
        except Exception:
            pass
        return False
    except Exception as e:
        _patch_state.update({'last_check_t': now, 'patched': False, 'last_error': str(e)})
        try:
            ui.message("Hermes session patch error: %s" % str(e)[:80])
        except Exception:
            pass
        return False


def auditHermesPatcher():
    """Return a dict describing the patcher's status. Cheap, no side effects."""
    import shutil
    out = {
        'patch_script': _find_patch_script(),
        'node_exe': _find_node_exe() or shutil.which('node'),
        'state': dict(_patch_state),
        'app_asar': os.path.join(
            os.path.expandvars(r'%LOCALAPPDATA%'),
            'hermes', 'hermes-agent', 'apps', 'desktop', 'release',
            'win-unpacked', 'resources', 'app.asar'),
    }
    script = out['patch_script']
    node = out['node_exe']
    if script and node:
        cf = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
        try:
            r = subprocess.run(
                [node, script, '--audit'],
                capture_output=True, text=True, timeout=15, creationflags=cf)
            if r.returncode == 0 and r.stdout.strip().startswith('{'):
                out['asar_audit'] = json.loads(r.stdout)
        except Exception as e:
            out['asar_audit_error'] = str(e)
    return out


def _resumeHermesSession(session_id, delay=0.0):
    """Switch session via hermes://session/<id> deep link.

    Opens a hermes:// URL which Windows routes to the running Hermes desktop
    app via the registered protocol handler. The main process's deep link
    handler detects kind='session' and sends 'hermes:focus-session' IPC to
    the renderer, which navigates to sessionRoute(sessionId).

    delay: ignored (deep link delivery is asynchronous, no sleep needed).
    """
    # Self-heal the app.asar patch (re-checks every call, 60s TTL cache).
    # We fire the deep link regardless: if the patch is missing the user
    # will see "still on old session" and we already announced the failure
    # via ui.message inside _ensureAppAsarPatched.
    if not _ensureAppAsarPatched(verbose=True):
        log.warning("hermes session resume fired with patch missing — user notified")

    cmd = 'hermes://session/' + str(session_id)
    try:
        import ctypes
        shell32 = ctypes.windll.shell32
        # ShellExecuteW returns HINSTANCE (64-bit on x64); set restype
        # so the return isn't truncated to 32-bit c_int.
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        SW_SHOWNORMAL = 1
        result = shell32.ShellExecuteW(None, "open", cmd, None, None, SW_SHOWNORMAL)
        # Return values <= 32 indicate an error (see ShellExecute docs).
        if result is not None and int(result) <= 32:
            errors = {
                0: "out of memory", 2: "file not found",
                3: "path not found", 5: "access denied",
                8: "not enough memory", 26: "sharing violation",
                27: "association incomplete", 28: "DDE timeout",
                29: "DDE failed", 30: "DDE busy", 31: "no association",
                32: "DLL not found",
            }
            ui.message("Resume failed: %s" % errors.get(int(result), "error %d" % int(result)))
            return False
        return True
    except Exception as e:
        ui.message("Resume failed: %s" % str(e)[:80])
        return False


# ═══════════════════════════════════════════════════════════════════
#  Global plugin
# ═══════════════════════════════════════════════════════════════════

class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    scriptCategory = "Hermes Accessibility"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._auto_timer = None
        self._last_nav_time = 0
        self._auto_interval_ms = 0
        # Foreground-watcher timer runs constantly (every 5s) so we can
        # detect when Hermes becomes foreground and start the auto-refresh
        # even before the user has pressed a hotkey. Without this, the
        # user had to use NVDA+Alt+N once just to "wake up" the streaming
        # reader. Now as soon as the agent starts writing to the temp
        # file, the watcher fires up the 1s auto-refresh.
        self._fg_timer = None
        self._last_fg_hermes = False
        self._startForegroundWatcher()
        # ui.message("Hermes nav loaded")  # silent

    def terminate(self):
        self._stopAutoRefresh()
        self._stopForegroundWatcher()
        super().terminate()

    # ── Foreground watcher (always-on) ──────────────────────────
    # This timer is the user's "set and forget" trigger: it runs as long
    # as the addon is loaded, polling every 5s to see whether Hermes is
    # the foreground app. When it is, the auto-refresh kicks in. When the
    # user switches away, the watcher is the only thing still running.
    # The cost is one cheap UI Automation call per 5s — negligible.

    def _startForegroundWatcher(self):
        if self._fg_timer is not None or wx is None:
            return
        try:
            self._fg_timer = wx.Timer()
            self._fg_timer.notify = self._onForegroundTick
            self._fg_timer.Start(5000)
        except Exception:
            self._fg_timer = None

    def _stopForegroundWatcher(self):
        if self._fg_timer is not None:
            try:
                self._fg_timer.Stop()
                self._fg_timer = None
            except Exception:
                pass

    def _onForegroundTick(self, event=None):
        is_hermes = _isHermesFg()
        if is_hermes and not self._last_fg_hermes:
            # Hermes just became foreground — start the auto-refresh
            # so the user can hit any message-nav hotkey without first
            # having to "prime" it. This is the bug the user reported:
            # "messages don't show until the agent is completely done"
            # — they were opening Hermes and pressing NVDA+Alt+N
            # before any auto-refresh had run, so the addon's _current_sid
            # was unset and the fetch returned no messages.
            if self._auto_timer is None:
                self._last_nav_time = time.time()
                self._startAutoRefresh()
        self._last_fg_hermes = is_hermes

    # ── Auto-refresh timer ──────────────────────────────────────

    def _startAutoRefresh(self):
        if self._auto_timer is not None or wx is None:
            return
        self._auto_timer = wx.Timer()
        self._auto_timer.notify = self._onAutoRefresh
        # Start at the streaming interval (1s) so a hotkey press that
        # immediately fires up the timer catches up on partial streaming
        # content fast. The handler bumps this up to the relaxed 3s
        # interval when the agent is idle.
        self._auto_timer.Start(1000)
        self._auto_interval_ms = 1000

    def _setAutoInterval(self, ms):
        """Switch the auto-refresh timer to a new interval. No-op if the
        timer is not running or the interval is unchanged."""
        if self._auto_timer is None or wx is None:
            return
        if getattr(self, "_auto_interval_ms", 0) == ms:
            return
        self._auto_timer.Stop()
        self._auto_timer.Start(ms)
        self._auto_interval_ms = ms

    def _stopAutoRefresh(self):
        if self._auto_timer is not None:
            try:
                self._auto_timer.Stop()
                self._auto_timer = None
            except Exception:
                pass

    def _onAutoRefresh(self, event=None):
        # Don't stop the timer while the agent is actively streaming
        # OR Hermes is the foreground app — the user is reading the
        # live response. Only auto-stop when ALL of: the user has been
        # idle for >90s, no streaming is in progress, and Hermes is not
        # the foreground app.
        idle_for = time.time() - self._last_nav_time
        if (idle_for > 90
                and not _mgr._streaming_active
                and not _isHermesFg()):
            self._stopAutoRefresh()
            return
        # Match the timer to the current state: 1s while streaming so
        # the user gets fresh content, 3s otherwise so we're not wasting
        # cycles once the agent is done.
        if _mgr._streaming_active:
            self._setAutoInterval(1000)
        else:
            self._setAutoInterval(3000)
        # Check if the active session in the desktop app has changed
        _mgr._detect_current_session()
        _mgr.refresh_messages(force=False)

    def _touch(self):
        """Mark navigation activity; start auto-refresh if needed."""
        self._last_nav_time = time.time()
        if self._auto_timer is None:
            self._startAutoRefresh()

    # ── Message navigation ──────────────────────────────────────

    def _speakMessage(self, index):
        msg = _mgr.get_message(index)
        if msg is None:
            ui.message("No message at %d (total: %d)" % (index + 1, _mgr.msg_count))
            return
        _mgr.msg_index = index
        # Truncate but keep the streaming note if it's the live chunk
        is_streaming = _mgr._streaming_active and index == _mgr.msg_count - 1
        if len(msg) > 3000:
            msg = msg[:3000] + " ... [truncated]"
        if is_streaming:
            # Prefix the message with a brief "still streaming" cue so
            # the user knows the response is incomplete. We use a
            # speech-level marker that NVDA will speak but the message
            # content itself stays unchanged.
            speech.speakMessage("Streaming, %d characters. " % len(msg))
        speech.speakText(msg)

    def _ensure_messages(self, force=False):
        """Make sure the current session's messages are loaded.

        force=True bypasses the throttle so every hotkey press reads the
        latest streaming content. force=False (auto-refresh path) honours
        the throttle to avoid hammering the DB."""
        _mgr.refresh_sessions()
        if _mgr.session_count == 0:
            return False
        # Detect if the active session in the desktop app has changed
        # (e.g. user clicked a different session in the sidebar)
        _mgr._detect_current_session()
        _mgr.refresh_messages(force=force)
        return _mgr.msg_count > 0

    def nextMessage(self):
        self._touch()
        if not self._ensure_messages(force=True):
            err = _mgr._error or ''
            ui.message("No messages found" + (" (%s)" % err if err else ""))
            return
        idx = _mgr.msg_index
        if idx < 0:
            # First press — read the last (most recent) message instead
            # of just announcing "Last message". Without this, NVDA+Alt+N
            # would never read a freshly-loaded streaming message on the
            # first press; the user had to use NVDA+Alt+P to actually
            # hear the content.
            self._speakMessage(_mgr.msg_count - 1)
            return
        if idx + 1 < _mgr.msg_count:
            self._speakMessage(idx + 1)
        else:
            ui.message("Last message (%d total)" % _mgr.msg_count)

    def prevMessage(self):
        self._touch()
        if not self._ensure_messages(force=True):
            err = _mgr._error or ''
            ui.message("No messages found" + (" (%s)" % err if err else ""))
            return
        idx = _mgr.msg_index
        if idx < 0:
            idx = _mgr.msg_count
        if idx > 0:
            self._speakMessage(idx - 1)
        else:
            ui.message("First message")

    def readLastMessage(self):
        self._touch()
        if not self._ensure_messages(force=True):
            err = _mgr._error or ''
            ui.message("No messages found" + (" (%s)" % err if err else ""))
            return
        self._speakMessage(_mgr.msg_count - 1)

    def firstMessage(self):
        # Added for parity with the OpenCode backend (NVDA+Alt+Home). Force a
        # fresh fetch in case the user just switched sessions, then speak the
        # first message in chronological order. Mirrors readLastMessage's
        # shape exactly except it targets index 0 instead of msg_count-1.
        self._touch()
        if not self._ensure_messages(force=True):
            err = _mgr._error or ''
            ui.message("No messages found" + (" (%s)" % err if err else ""))
            return
        self._speakMessage(0)

    def readCurrentMessage(self):
        self._touch()
        if not self._ensure_messages(force=True):
            ui.message("No messages found")
            return
        idx = _mgr.msg_index
        if idx < 0:
            idx = _mgr.msg_count - 1
        self._speakMessage(idx)

    # ── Session browsing ────────────────────────────────────────

    def pickSession(self):
        """Open session picker dialog (deferred via wx.CallAfter)."""
        if wx is not None:
            wx.CallAfter(_doShowPicker)

    def nextSession(self):
        """Quick-switch to next session (sends /resume to Hermes)."""
        ok = _mgr.select_next()
        if not ok:
            ui.message("No sessions available")
            return
        s = _mgr.current_session
        if s:
            ui.message("[%d/%d] %s — %s (%d msgs)" % (
                _mgr.session_index + 1, _mgr.session_count,
                _fmt_time(s['started_at']), s['title'][:50],
                s.get('message_count', 0)))
            # Load messages from the new session so msg nav follows immediately
            _mgr._do_refresh_messages()
            # Defer the resume so speech isn't blocked by clipboard/sleep
            if wx is not None:
                wx.CallAfter(_resumeHermesSession, s['id'])
            else:
                _resumeHermesSession(s['id'])

    def prevSession(self):
        """Quick-switch to previous session (sends /resume to Hermes)."""
        ok = _mgr.select_prev()
        if not ok:
            ui.message("No sessions available")
            return
        s = _mgr.current_session
        if s:
            ui.message("[%d/%d] %s — %s (%d msgs)" % (
                _mgr.session_index + 1, _mgr.session_count,
                _fmt_time(s['started_at']), s['title'][:50],
                s.get('message_count', 0)))
            # Load messages from the new session so msg nav follows immediately
            _mgr._do_refresh_messages()
            if wx is not None:
                wx.CallAfter(_resumeHermesSession, s['id'])
            else:
                _resumeHermesSession(s['id'])

    # ── Diagnostics ─────────────────────────────────────────────

    def announceMessageInfo(self):
        if not self._ensure_messages(force=True):
            ui.message("No messages")
            return
        cur = _mgr.msg_index
        if cur < 0 or cur >= _mgr.msg_count:
            cur = _mgr.msg_count - 1
        s = _mgr.current_session
        header = ""
        if s:
            header = "%s | " % s['title'][:40]
        streaming_note = ""
        if _mgr._streaming_active:
            streaming_note = " (streaming)"
        lines = ["%sMessage %d of %d%s" % (
            header, cur + 1, _mgr.msg_count, streaming_note)]
        for i in range(max(0, cur - 1), min(_mgr.msg_count, cur + 2)):
            marker = ">>>" if i == cur else "   "
            preview = _mgr.messages[i][:70].replace('\n', ' ')
            lines.append("%s [%d] %s" % (marker, i + 1, preview))
        ui.message('\n'.join(lines))

    def dumpMessages(self):
        _mgr.refresh_sessions()
        _mgr.refresh_messages()
        py = _python()
        lines = [
            "Python: %s" % (py or "NOT FOUND"),
            "DB: %s (exists=%s)" % (_HERMES_DB, os.path.exists(_HERMES_DB)),
            "Sessions: %d, current: [%d] %s" % (
                _mgr.session_count, _mgr.session_index + 1,
                (_mgr.current_session or {}).get('title', '?')[:50]),
            "Messages: %d loaded" % _mgr.msg_count,
        ]
        if _mgr._error:
            lines.append("Last error: %s" % _mgr._error)
        for i, t in enumerate(_mgr.messages[:8]):
            lines.append("  [%d] %s" % (i + 1, t[:80].replace('\n', ' ')))
        if _mgr.msg_count > 8:
            lines.append("  ... and %d more" % (_mgr.msg_count - 8))
        ui.message('\n'.join(lines))

    def refreshNow(self):
        """Force a fresh fetch right now, bypassing any throttle.

        Useful when the user wants the absolute latest content (e.g.
        a long streaming response that they haven't refreshed in a
        while) without waiting for the next auto-refresh tick."""
        self._touch()
        if not self._ensure_messages(force=True):
            ui.message("No messages to refresh")
            return
        # If we're on the last message, re-speak it so the user
        # immediately hears the freshest content rather than just
        # seeing the count update.
        if _mgr.msg_count > 0 and (
            _mgr.msg_index < 0
            or _mgr.msg_index == _mgr.msg_count - 1
        ):
            self._speakMessage(_mgr.msg_count - 1)
        else:
            s = _mgr.current_session
            title = (s or {}).get('title', '?')[:50]
            streaming_note = " (streaming)" if _mgr._streaming_active else ""
            ui.message(
                "Refreshed: %d messages, position %d of %d%s. %s" % (
                    _mgr.msg_count,
                    (_mgr.msg_index + 1) if _mgr.msg_index >= 0 else 0,
                    _mgr.msg_count, streaming_note, title)
            )

    # ── Gestures ────────────────────────────────────────────────

