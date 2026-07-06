# -*- coding: UTF-8 -*-
# globalPlugins/_hermes.py
#
# Hermes backend facade for agentDesktopAccessibility.
#
# The dispatcher in _plugin.py talks to a single HermesBackend object. The
# facade wires together the three existing Hermes modules:
#   - hermesMessageNav    — message nav + session browsing (uses sqlite3 via subprocess)
#   - hermesCompletion  — @ reference picker dialog (uses wx, paste via keybd_event)
#   - hermesSpeechFilter — synth-driver speech filter (suppresses status spam)
#
# Each module's GlobalPlugin (or module-level functions) is instantiated/used
# via thin methods here. The dispatcher never touches those modules directly.
#
# This facade is also where we install the synth hook once at startup and
# remove it on terminate — that way the speech filter is always active while
# the add-on is loaded, regardless of which keystroke the user presses.

import api
import ui
from . import hermesMessageNav
from . import hermesCompletion
from . import hermesSpeechFilter


class HermesBackend(object):
    """Single entry point for all Hermes-specific behaviour.

    Constructed once by the top-level GlobalPlugin in _plugin.py.
    """

    def __init__(self):
        # The msg module's GlobalPlugin holds the auto-refresh timer,
        # foreground watcher, and all message-nav state. We instantiate it
        # but skip its super().__init__ chain (it doesn't have one — its
        # parent class is globalPluginHandler.GlobalPlugin but it's never
        # registered with NVDA, so the parent __init__ is fine to call).
        self._msg = hermesMessageNav.GlobalPlugin()
        # The speech filter installs a synth hook. We do it eagerly so the
        # filter is active the moment the add-on loads, not on first keystroke.
        # We also instantiate the GlobalPlugin ONCE here so the toggle/status
        # methods have a stable `self` to bind to (and so the timer-retry /
        # maintenance timers are managed by exactly one instance).
        self._speech_plugin = hermesSpeechFilter.GlobalPlugin()
        # The at-ref module is a collection of functions, no instance needed.
        self._atref = hermesCompletion
        self._speech = hermesSpeechFilter

    # ── Message navigation ──────────────────────────────────────

    def nextMessage(self):
        self._msg.nextMessage()

    def prevMessage(self):
        self._msg.prevMessage()

    def firstMessage(self):
        # Added in Task 7 — see hermesMessageNav.py for the implementation.
        self._msg.firstMessage()

    def lastMessage(self):
        # The original Hermes plugin had no `lastMessage`; the equivalent
        # was `readLastMessage` (jumped to msg_count-1). We expose that as
        # `lastMessage` for parity with the OpenCode backend.
        self._msg.readLastMessage()

    def readCurrentMessage(self):
        self._msg.readCurrentMessage()

    def refreshNow(self):
        # Original Hermes had `refreshNow` (NVDA+Alt+R). The new dispatcher
        # routes NVDA+Alt+R to the OpenCode backend when OpenCode is foreground,
        # and to Hermes.readCurrentMessage when Hermes is foreground. This
        # method is a stronger "force fresh fetch" used by the dispatcher
        # in some paths.
        self._msg.refreshNow()

    # ── Session management ──────────────────────────────────────

    def pickSession(self):
        self._msg.pickSession()

    def nextSession(self):
        self._msg.nextSession()

    def prevSession(self):
        self._msg.prevSession()

    def newSession(self):
        # Task 9: send Ctrl+N to Hermes desktop. The dispatcher's
        # NVDA+Alt+Ctrl+N gesture is bound to this.
        try:
            import ctypes
            user32 = ctypes.windll.user32
            KEYEVENTF_KEYUP = 0x0002
            # Ctrl down, N down
            user32.keybd_event(0x11, 0, 0, 0)
            user32.keybd_event(0x4E, 0, 0, 0)
            # N up, Ctrl up
            user32.keybd_event(0x4E, 0, KEYEVENTF_KEYUP, 0)
            user32.keybd_event(0x11, 0, KEYEVENTF_KEYUP, 0)
            ui.message("New session")
        except Exception as e:
            ui.message("New session failed: %s" % e)

    def dumpMessages(self):
        self._msg.dumpMessages()

    def auditPatcher(self):
        """Return a dict describing the Hermes app.asar patch status.

        Used by the dispatcher's NVDA+Alt+Shift+D diagnostic so the user
        can quickly tell whether the deep-link patch is in place after
        a Hermes update.
        """
        return self._msg.auditHermesPatcher()

    # ── Preserved Hermes-specific gestures ──────────────────────
    #
    # The user explicitly asked for the speech filter and @ reference picker
    # to survive the merge. These three methods back the preserved bindings:
    #   NVDA+Alt+Space  -> show_at_picker  (the @ reference picker)
    #   NVDA+Shift+H    -> toggle_filter   (toggle speech filter on/off)
    #   NVDA+Shift+J    -> filter_status   (announce status + count)

    def showAtPicker(self):
        # CRITICAL: must defer the dialog to the next wx event-loop tick.
        # Calling ShowModal() directly from a gesture handler crashes NVDA
        # (nested event loop collides with NVDA's hook procedures). The
        # original hermesCompletion.script_showAtCompletion() did this via
        # `wx.CallAfter(_doShowAtCompletion)`; the dispatcher's @script
        # wrapper does NOT call wx.CallAfter for us, so we have to do it
        # here.
        try:
            import wx
            wx.CallAfter(self._atref._doShowAtCompletion)
        except Exception as e:
            try:
                import ui
                ui.message("Could not open @ reference picker: %s" % e)
            except Exception:
                pass

    def toggleFilter(self):
        # Route through the cached plugin instance — don't create a new
        # one on every keystroke (that would leak synth hooks and start
        # extra timer threads).
        self._speech_plugin.toggle_filter()

    def filterStatus(self):
        self._speech_plugin.filter_status()

    # ── Lifecycle ───────────────────────────────────────────────

    def terminate(self):
        try:
            self._msg.terminate()
        except Exception:
            pass
        try:
            self._speech_plugin.terminate()
        except Exception:
            pass
        try:
            self._speech._uninstallHook()
        except Exception:
            pass
