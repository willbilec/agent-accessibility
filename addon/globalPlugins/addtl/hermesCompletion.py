# -*- coding: UTF-8 -*-
# globalPlugins/hermesCompletion.py
#
# Accessible @ context-reference completion for Hermes Agent.
#
# Press NVDA+Alt+Space while focused in Hermes to open a two-pane
# dialog: left pane shows reference types (@file:, @folder:, etc.),
# right pane shows recently used items for the selected type.
# Browse to add new folders/files, or type a URL directly.
#
# Gestures:
#   NVDA+Alt+Space   — Open @ reference picker

import globalPluginHandler
import api
import ui
import os
import re
import json
import time

try:
    import wx
except ImportError:
    wx = None

# ── History persistence ────────────────────────────────────────────

def _history_path():
    addon_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(addon_dir, "atref_history.json")

def _load_history():
    path = _history_path()
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            for key in ('folders', 'files', 'urls'):
                if key not in data:
                    data[key] = []
            return data
    except Exception:
        pass
    return {'folders': [], 'files': [], 'urls': []}

def _save_history(data):
    path = _history_path()
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def _add_to_history(category, value):
    data = _load_history()
    items = data.get(category, [])
    if value in items:
        items.remove(value)
    items.insert(0, value)
    data[category] = items[:30]
    _save_history(data)


def _displayForPath(value):
    """Pretty label for a path stored in history: "<basename> — <path>".

    The full path is still what we send through `_pasteIntoHermes` and persist
    in `atref_history.json`; this is purely for the right-pane ListBox so the
    user can tell two folders with the same basename apart at a glance.

    URLs (which have no meaningful basename) and short values pass through
    unchanged.
    """
    if not value:
        return value
    if "://" in value:
        return value
    base = os.path.basename(value.rstrip("/\\"))
    if not base or base == value:
        return value
    return "%s — %s" % (base, value)


# Mirrors the desktop side's `formatRefValue` in directive-text.tsx. Paths
# with whitespace or quoting-sensitive characters MUST be wrapped in
# backticks (or another delimiter) on the wire, otherwise the parser's `\\S+`
# alternative only matches up to the first space and the agent receives a
# truncated path (e.g. `@folder:C:/Users/willb/programs/Hermes` for the real
# `C:/Users/willb/programs/Hermes accessibility`).
def _quoteRefValue(value):
    if not value:
        return value
    # Anything other than a run of "safe" chars needs quoting. The character
    # class matches the desktop's `needsQuoting` regex exactly.
    if not re.search(r"[\s()\[\]{}<>\"'`]", value):
        return value
    # Cascade through delimiters in the same preference order as the desktop:
    # backticks first (matches the canonical form), then double quotes, then
    # single quotes, then raw as a last resort.
    if "`" not in value:
        return "`%s`" % value
    if '"' not in value:
        return '"%s"' % value
    if "'" not in value:
        return "'%s'" % value
    return value


# ── Foreground detection ────────────────────────────────────────────

def _isHermesForeground():
    try:
        fg = api.getForegroundObject()
        if fg is not None:
            appMod = getattr(fg, 'appModule', None)
            if appMod is not None:
                name = getattr(appMod, 'appName', '') or ''
                return 'hermes' in name.lower()
    except Exception:
        pass
    return False


# ── Reference type definitions ──────────────────────────────────────
# (ref_text, label, history_key, has_browse, allow_text_input)

# Folder first because the most common @-mention is a directory the user wants
# the agent to read or act on. The right-pane "Recent items" list also defaults
# to the recent-folder list, which is the highest-traffic entry point.
_REF_TYPES = [
    ("@folder:", "Folder",       "folders",   True,  False),
    ("@file:",   "File",         "files",     True,  False),
    ("@url:",    "URL",          "urls",      False, True),
    ("@diff",    "Git diff",     None,        False, False),
    ("@staged",  "Git staged",   None,        False, False),
    ("@git:",    "Git log (N commits)", None, False, False),
]


# ═══════════════════════════════════════════════════════════════════
#  Two-pane picker dialog
# ═══════════════════════════════════════════════════════════════════

class _AtRefDialog(wx.Dialog):
    """Two-pane dialog: types on left, recent items on right."""

    def __init__(self, parent):
        super().__init__(
            parent, title="Hermes @ Reference",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._types = _REF_TYPES
        self._history = _load_history()
        self._text_input = None
        self._browse_btn = None
        self._browse_label = ""

        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # Header
        hdr = wx.StaticText(panel, label="Choose a reference type (left) then pick or browse (right):")
        hdr_font = hdr.GetFont()
        hdr_font.SetWeight(wx.FONTWEIGHT_BOLD)
        hdr.SetFont(hdr_font)
        main_sizer.Add(hdr, 0, wx.ALL, 10)

        # Two-pane body
        body = wx.BoxSizer(wx.HORIZONTAL)

        # ── Left pane: reference types ──
        left_sizer = wx.BoxSizer(wx.VERTICAL)
        left_label = wx.StaticText(panel, label="Reference type:")
        left_sizer.Add(left_label, 0, wx.BOTTOM, 4)

        type_names = [t[1] for t in self._types]
        self._type_list = wx.ListBox(panel, choices=type_names, style=wx.LB_SINGLE)
        self._type_list.SetSelection(0)
        left_sizer.Add(self._type_list, 1, wx.EXPAND)

        body.Add(left_sizer, 1, wx.EXPAND | wx.ALL, 5)

        # ── Right pane: recent items ──
        right_sizer = wx.BoxSizer(wx.VERTICAL)
        right_label = wx.StaticText(panel, label="Recent items:")
        self._right_label = right_label
        right_sizer.Add(right_label, 0, wx.BOTTOM, 4)

        self._item_list = wx.ListBox(panel, choices=[], style=wx.LB_SINGLE)
        right_sizer.Add(self._item_list, 1, wx.EXPAND)

        # URL text input (hidden by default)
        self._text_input = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self._text_input.Hide()
        right_sizer.Add(self._text_input, 0, wx.EXPAND | wx.TOP, 5)

        body.Add(right_sizer, 2, wx.EXPAND | wx.ALL, 5)

        main_sizer.Add(body, 1, wx.EXPAND | wx.ALL, 5)

        # ── Bottom buttons ──
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self._browse_btn = wx.Button(panel, wx.ID_OPEN, "&Browse...")
        btn_sizer.Add(self._browse_btn, 0, wx.RIGHT, 5)

        btn_sizer.AddStretchSpacer()

        insert_btn = wx.Button(panel, wx.ID_OK, "&Insert")
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        btn_sizer.Add(insert_btn, 0, wx.RIGHT, 5)
        btn_sizer.Add(cancel_btn, 0)

        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 10)

        panel.SetSizer(main_sizer)
        self.SetInitialSize(wx.Size(700, 440))
        self.Centre()

        # ── Bindings ──
        self._type_list.Bind(wx.EVT_LISTBOX, self._onTypeChanged)
        self._type_list.Bind(wx.EVT_CHAR_HOOK, self._onTypeKey)
        self._item_list.Bind(wx.EVT_CHAR_HOOK, self._onItemKey)
        self._item_list.Bind(wx.EVT_LISTBOX_DCLICK, lambda e: self.EndModal(wx.ID_OK))
        self._text_input.Bind(wx.EVT_TEXT_ENTER, self._onTextEnter)
        self._browse_btn.Bind(wx.EVT_BUTTON, self._onBrowse)
        insert_btn.SetDefault()

        # Populate right pane for the first type
        self._updateRightPane(0)

        self._type_list.SetFocus()

    # ── Right pane management ──

    def _currentTypeInfo(self):
        idx = self._type_list.GetSelection()
        if 0 <= idx < len(self._types):
            return self._types[idx]
        return self._types[0]

    def _updateRightPane(self, type_idx):
        ref_text, label, hist_key, has_browse, allow_text = self._types[type_idx]

        self._right_label.SetLabel("Recent %ss:" % label.lower())

        # Keep the raw history on self so GetResult can map the user's
        # display-label selection back to the full path the agent expects.
        self._history = _load_history()
        self._history_display = []

        # Update item list
        if hist_key:
            raw_items = self._history.get(hist_key, [])
            self._history_display = [_displayForPath(v) for v in raw_items]
        else:
            raw_items = []
        self._item_list.Set(self._history_display)
        if self._history_display:
            self._item_list.SetSelection(0)

        # Show/hide text input
        if allow_text:
            self._text_input.Show()
            self._text_input.SetValue("")
        else:
            self._text_input.Hide()

        # Show/hide browse button
        if has_browse:
            self._browse_btn.Show()
            if label == "File":
                self._browse_btn.SetLabel("&Browse for file...")
            elif label == "Folder":
                self._browse_btn.SetLabel("&Browse for folder...")
        else:
            self._browse_btn.Hide()

        self.Layout()

    def _onTypeKey(self, event):
        key = event.GetKeyCode()
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self.EndModal(wx.ID_OK)
        else:
            event.Skip()

    def _onTypeChanged(self, event):
        self._updateRightPane(event.GetSelection())

    def _onItemKey(self, event):
        key = event.GetKeyCode()
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self.EndModal(wx.ID_OK)
        else:
            event.Skip()

    def _onTextEnter(self, event):
        text = self._text_input.GetValue().strip()
        if text:
            self.EndModal(wx.ID_OK)

    def _onBrowse(self, event):
        _, label, _, _, _ = self._currentTypeInfo()
        if label == "File":
            path = _browseFile()
        elif label == "Folder":
            path = _browseFolder()
        else:
            return

        if path:
            # Add to the in-memory history + the parallel display list, then
            # refresh the right-pane listbox with the pretty "name — path" rows.
            hist_key = self._currentTypeInfo()[2]
            if hist_key:
                raw_items = self._history.get(hist_key, [])
                if path not in raw_items:
                    raw_items.insert(0, path)
                else:
                    raw_items.remove(path)
                    raw_items.insert(0, path)
                self._history[hist_key] = raw_items
                self._history_display = [_displayForPath(v) for v in raw_items]
                self._item_list.Set(self._history_display)
                self._item_list.SetSelection(0)

    # ── Result ──

    def GetResult(self):
        """Return the final @reference text, or None."""
        type_idx = self._type_list.GetSelection()
        if type_idx < 0:
            return None

        ref_prefix, label, hist_key, has_browse, allow_text = self._types[type_idx]

        # For simple refs (no value needed)
        if hist_key is None and not allow_text:
            # @diff, @staged
            return ref_prefix

        # For @git: — prompt for count
        if ref_prefix == "@git:":
            return _promptGitCount(self)

        # Get the value from list or text input
        value = None

        if allow_text:
            # URL: check text input first, then list
            text_val = self._text_input.GetValue().strip()
            if text_val:
                value = text_val

        if value is None:
            idx = self._item_list.GetSelection()
            if 0 <= idx < self._item_list.GetCount():
                selected_display = self._item_list.GetString(idx)
                # Map the pretty "name — path" display label back to the raw
                # path the agent expects. `_history_display` is built in the
                # same order as the persisted history, so a parallel lookup is
                # sufficient.
                if hist_key and 0 <= idx < len(self._history_display) and self._history_display[idx] == selected_display:
                    raw_items = self._history.get(hist_key, [])
                    if 0 <= idx < len(raw_items):
                        value = raw_items[idx]
                    else:
                        value = selected_display
                else:
                    value = selected_display

        if not value:
            return None

        # Normalize
        value = value.replace("\\", "/")

        # Ensure URL has scheme
        if ref_prefix == "@url:" and "://" not in value:
            value = "https://" + value

        # Save to history
        if hist_key:
            _add_to_history(hist_key, value)

        # Wrap the value in backticks if it contains whitespace or any other
        # character the directive parser's `\\S+` alternative would stop on.
        # Without this, a folder like "C:/Users/willb/programs/Hermes
        # accessibility" is truncated to "C:/Users/willb/programs/Hermes" the
        # moment the agent sees it, and the lookup fails.
        return ref_prefix + _quoteRefValue(value)


# ── Browse helpers ────────────────────────────────────────────────

def _browseFile():
    if wx is None:
        return None
    dlg = wx.FileDialog(
        None, message="Choose a file to attach",
        defaultDir=os.path.expanduser("~"),
        style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST
    )
    try:
        if dlg.ShowModal() == wx.ID_OK:
            return dlg.GetPath()
    finally:
        dlg.Destroy()
    return None


def _browseFolder():
    if wx is None:
        return None
    dlg = wx.DirDialog(
        None, message="Choose a folder to attach",
        defaultPath=os.path.expanduser("~"),
        style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST
    )
    try:
        if dlg.ShowModal() == wx.ID_OK:
            return dlg.GetPath()
    finally:
        dlg.Destroy()
    return None


def _promptGitCount(parent):
    if wx is None:
        return "@git:5"
    dlg = wx.TextEntryDialog(
        parent, "Number of recent commits to include:",
        "Hermes @ Git Reference", "5",
        style=wx.OK | wx.CANCEL
    )
    try:
        if dlg.ShowModal() == wx.ID_OK:
            count = dlg.GetValue().strip()
            if count.isdigit() and int(count) > 0:
                return "@git:" + count
            ui.message("Invalid, using 5")
    finally:
        dlg.Destroy()
    return "@git:5"


# ── Text injection ─────────────────────────────────────────────────

def _pasteIntoHermes(text):
    if not text:
        return False
    if not wx.TheClipboard.Open():
        ui.message("Cannot open clipboard")
        return False
    try:
        wx.TheClipboard.SetData(wx.TextDataObject(text))
    finally:
        wx.TheClipboard.Close()

    time.sleep(0.05)

    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.keybd_event(0x11, 0, 0, 0)        # Ctrl down
        user32.keybd_event(0x56, 0, 0, 0)        # V down
        user32.keybd_event(0x56, 0, 0x0002, 0)   # V up
        user32.keybd_event(0x11, 0, 0x0002, 0)   # Ctrl up
        preview = text[:60] + ("..." if len(text) > 60 else "")
        ui.message("Inserted: %s" % preview)
        return True
    except Exception as e:
        ui.message("Paste failed (in clipboard): %s" % str(e)[:80])
        return False


# ── Main entry point ────────────────────────────────────────────────

def _doShowAtCompletion():
    import gui

    gui.mainFrame.prePopup()
    ref_text = None

    try:
        dlg = _AtRefDialog(gui.mainFrame)
        if dlg.ShowModal() == wx.ID_OK:
            ref_text = dlg.GetResult()
        dlg.Destroy()
    finally:
        gui.mainFrame.postPopup()

    if ref_text:
        _pasteIntoHermes(ref_text)


# ═══════════════════════════════════════════════════════════════════
#  Global plugin
# ═══════════════════════════════════════════════════════════════════

class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    scriptCategory = "Hermes Accessibility"

    def script_showAtCompletion(self, gesture):
        if not _isHermesForeground():
            ui.message("Not in Hermes")
            return
        if wx is not None:
            wx.CallAfter(_doShowAtCompletion)

    # NB: __gestures was removed in agentDesktopAccessibility 2.0.0. Was:
    #     __gestures = {
    #         "kb:NVDA+alt+space": "showAtCompletion",
    #     }
    # The dispatcher in agentDesktopAccessibility.py binds NVDA+Alt+Space
    # on its own script_atRefPicker wrapper and routes to
    # hermesBackend.showAtPicker() → _doShowAtCompletion() directly.
