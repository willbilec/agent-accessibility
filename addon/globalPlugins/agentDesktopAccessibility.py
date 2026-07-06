# -*- coding: UTF-8 -*-
# globalPlugins/_plugin.py
#
# Top-level NVDA global plugin for agentDesktopAccessibility.
#
# This is the ONLY file that registers @script decorators / gestures. The
# two backend modules (_hermes.py and _opencode.py) are plain Python
# classes; they don't know about NVDA's gesture system.
#
# Hotkey set (foreground-aware; same binding does the right thing in each app):
#
#   Shared (Hermes OR OpenCode):
#     kb:NVDA+alt+downArrow   next message
#     kb:NVDA+alt+upArrow     previous message
#     kb:NVDA+alt+home        first message
#     kb:NVDA+alt+end         last message
#     kb:NVDA+alt+r           re-read current message
#     kb:NVDA+alt+s           open session switcher
#     kb:NVDA+alt+shift+n     next session
#     kb:NVDA+alt+shift+p     previous session
#     kb:NVDA+alt+d           diagnostic dump
#
#   Single global new-session binding:
#     kb:control+n            new session
#       - OpenCode: triggers the add-on's 5-method fallback chain
#         (button activation → API → bridge file → clipboard → Ctrl+N)
#       - Hermes: gesture.send() — Hermes handles Ctrl+N natively
#       - Any other app: gesture.send() — OS delivers Ctrl+N normally
#     (This replaces the old NVDA+Alt+Ctrl+N from the Hermes add-on.)
#
#   Always on:
#     kb:NVDA+alt+shift+d     foreground window metadata
#
#   OpenCode only (pass-through when Hermes is foreground):
#     kb:NVDA+alt+t           read thinking trace
#     kb:NVDA+alt+a           toggle auto-read
#
#   Hermes only — PRESERVED from the original hermesAccessibility 1.7.2
#   (pass-through when OpenCode is foreground):
#     kb:NVDA+alt+space       @ reference picker
#     kb:NVDA+shift+h         toggle speech filter
#     kb:NVDA+shift+j         speech filter status
#
# Removed in this release (the user asked for the OpenCode arrow/Home/End
# set to replace them):
#   NVDA+Alt+N/P/L/C/R  (Hermes message-nav letters)
#   NVDA+Alt+I          (Hermes position info)
#   NVDA+Alt+S in the Hermes sense still works — the dispatcher routes it
#   to the Hermes session picker when Hermes is foreground, the OpenCode
#   session switcher when OpenCode is foreground.
#
# Pass-through: any gesture above that doesn't apply to the current
# foreground calls gesture.send() so the keystroke isn't swallowed.

import globalPluginHandler
import ui
from scriptHandler import script
from logHandler import log

from .addtl.router import route
from .addtl.hermesBackend import HermesBackend
from .addtl.opencodeBackend import OpenCodeBackend


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    scriptCategory = "Agent Desktop Accessibility"

    def __init__(self):
        super().__init__()
        log.info("agentDesktopAccessibility 2.1.0 loading")
        self._hermes = HermesBackend()
        self._opencode = OpenCodeBackend(plugin=self)
        log.info("agentDesktopAccessibility 2.1.0 loaded — backends: hermes, opencode")
        # Self-heal the Hermes app.asar patch on load. Runs in a background
        # thread via wx so the keystroke handler isn't blocked. If the patch
        # is already in place, this is a single asar file read (<100ms).
        try:
            import wx
            wx.CallAfter(self._warmPatcher)
        except Exception:
            pass

    def _warmPatcher(self):
        try:
            from logHandler import log
            audit = self._hermes.auditPatcher()
            asar = audit.get('asar_audit') or {}
            if asar and not asar.get('patched'):
                log.info("agentDesktopAccessibility: hermes asar patch missing, applying")
                self._hermes._msg._ensureAppAsarPatched(verbose=True)
        except Exception as e:
            try:
                from logHandler import log
                log.warning("agentDesktopAccessibility: warmPatcher error: %s", e)
            except Exception:
                pass

    def terminate(self):
        try:
            self._hermes.terminate()
        except Exception as e:
            log.warning("agentDesktopAccessibility: hermes terminate error: %s", e)
        try:
            self._opencode.terminate()
        except Exception as e:
            log.warning("agentDesktopAccessibility: opencode terminate error: %s", e)
        super().terminate()

    # ─────────────────────────────────────────────────────────────
    # Shared message navigation — Hermes OR OpenCode
    # ─────────────────────────────────────────────────────────────

    @script(gesture="kb:NVDA+alt+downArrow",
            description="Next message (Hermes or OpenCode)")
    def script_nextMessage(self, gesture):
        target = route()
        if target == "hermes":
            self._hermes.nextMessage()
        elif target == "opencode":
            self._opencode.nextMessage()
        else:
            gesture.send()

    @script(gesture="kb:NVDA+alt+upArrow",
            description="Previous message (Hermes or OpenCode)")
    def script_previousMessage(self, gesture):
        target = route()
        if target == "hermes":
            self._hermes.prevMessage()
        elif target == "opencode":
            self._opencode.previousMessage()
        else:
            gesture.send()

    @script(gesture="kb:NVDA+alt+home",
            description="First message (Hermes or OpenCode)")
    def script_firstMessage(self, gesture):
        target = route()
        if target == "hermes":
            self._hermes.firstMessage()
        elif target == "opencode":
            self._opencode.firstMessage()
        else:
            gesture.send()

    @script(gesture="kb:NVDA+alt+end",
            description="Last message (Hermes or OpenCode)")
    def script_lastMessage(self, gesture):
        target = route()
        if target == "hermes":
            self._hermes.lastMessage()
        elif target == "opencode":
            self._opencode.lastMessage()
        else:
            gesture.send()

    @script(gesture="kb:NVDA+alt+r",
            description="Re-read current message (Hermes or OpenCode)")
    def script_readCurrentMessage(self, gesture):
        target = route()
        if target == "hermes":
            self._hermes.readCurrentMessage()
        elif target == "opencode":
            self._opencode.readCurrentMessage()
        else:
            gesture.send()

    # ─────────────────────────────────────────────────────────────
    # Session management — Hermes OR OpenCode
    # ─────────────────────────────────────────────────────────────

    @script(gesture="kb:NVDA+alt+s",
            description="Open session switcher (Hermes or OpenCode)")
    def script_sessionPicker(self, gesture):
        target = route()
        if target == "hermes":
            self._hermes.pickSession()
        elif target == "opencode":
            self._opencode.openSessionPicker()
        else:
            gesture.send()

    @script(gesture="kb:NVDA+alt+shift+n",
            description="Next session (Hermes or OpenCode)")
    def script_nextSession(self, gesture):
        target = route()
        if target == "hermes":
            self._hermes.nextSession()
        elif target == "opencode":
            self._opencode.nextSession()
        else:
            gesture.send()

    @script(gesture="kb:NVDA+alt+shift+p",
            description="Previous session (Hermes or OpenCode)")
    def script_previousSession(self, gesture):
        target = route()
        if target == "hermes":
            self._hermes.prevSession()
        elif target == "opencode":
            self._opencode.previousSession()
        else:
            gesture.send()

    @script(gesture="kb:control+n",
            description="New session: pass-through in Hermes (handled natively), triggers OpenCode's 5-method fallback in OpenCode, pass-through elsewhere")
    def script_newSession(self, gesture):
        # Single unified Ctrl+N binding.
        # - Hermes foreground: gesture.send() — Hermes handles Ctrl+N natively
        # - OpenCode foreground: invoke OpenCode's 5-method new-session fallback
        # - Anything else: gesture.send() — let the OS deliver Ctrl+N normally
        target = route()
        if target == "opencode":
            self._opencode.newSession()
        else:
            # Hermes or neither: pass through. The user already has Ctrl+N
            # wired up natively in Hermes, and the OS handles Ctrl+N in
            # every other app — the add-on should not intercept it.
            gesture.send()

    @script(gesture="kb:NVDA+alt+d",
            description="Diagnostic dump (Hermes or OpenCode)")
    def script_dump(self, gesture):
        target = route()
        if target == "hermes":
            self._hermes.dumpMessages()
            # Also surface patcher audit so the user can confirm the deep
            # link is in place after a Hermes update.
            try:
                audit = self._hermes.auditPatcher()
                asar = audit.get('asar_audit') or {}
                if asar:
                    ui.message("Patcher: asar %s, marker %s" % (
                        "OK" if asar.get('patched') else "MISSING",
                        "found" if asar.get('handleDeepLinkFound') else "not-found"))
            except Exception:
                pass
        elif target == "opencode":
            self._opencode.dumpDebug()
        else:
            gesture.send()

    # ─────────────────────────────────────────────────────────────
    # OpenCode-only — pass-through when Hermes is foreground
    # ─────────────────────────────────────────────────────────────

    @script(gesture="kb:NVDA+alt+t",
            description="Read thinking trace (OpenCode only)")
    def script_readThinking(self, gesture):
        if route() == "opencode":
            self._opencode.readThinking()
        else:
            gesture.send()

    @script(gesture="kb:NVDA+alt+a",
            description="Toggle auto-read (OpenCode only)")
    def script_toggleAutoRead(self, gesture):
        if route() == "opencode":
            self._opencode.toggleAutoRead()
        else:
            gesture.send()

    # ─────────────────────────────────────────────────────────────
    # Hermes-only — PRESERVED from hermesAccessibility 1.7.2
    # Pass-through when OpenCode is foreground
    # ─────────────────────────────────────────────────────────────

    @script(gesture="kb:NVDA+alt+space",
            description="@ reference picker (Hermes only) — preserved")
    def script_atRefPicker(self, gesture):
        if route() == "hermes":
            self._hermes.showAtPicker()
        else:
            gesture.send()

    @script(gesture="kb:NVDA+shift+h",
            description="Toggle Hermes speech filter — preserved")
    def script_toggleHermesFilter(self, gesture):
        if route() == "hermes":
            self._hermes.toggleFilter()
        else:
            gesture.send()

    @script(gesture="kb:NVDA+shift+j",
            description="Hermes speech filter status — preserved")
    def script_hermesFilterStatus(self, gesture):
        if route() == "hermes":
            self._hermes.filterStatus()
        else:
            gesture.send()

    # ─────────────────────────────────────────────────────────────
    # Always on — fires regardless of foreground
    # ─────────────────────────────────────────────────────────────

    @script(gesture="kb:NVDA+alt+shift+d",
            description="Foreground window metadata (always on)")
    def script_describeForeground(self, gesture):
        # OpenCode has the heavy-weight _detectForeground (processPath via
        # ctypes). Reuse it; the other branch uses the lightweight router
        # info, which is enough for the "what's foreground?" question.
        target = route()
        if target == "opencode":
            self._opencode.describeForeground()
        elif target == "hermes":
            title = self._opencode._detectForeground().get("title") or "(no title)"
            ui.message("Foreground: Hermes — %s" % title)
        else:
            info = self._opencode._detectForeground()
            ui.message("Foreground: %s" % (info.get("title") or "(no title)"))
