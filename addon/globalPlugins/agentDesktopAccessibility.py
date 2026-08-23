# -*- coding: UTF-8 -*-
# globalPlugins/_plugin.py
#
# Top-level NVDA global plugin for agentDesktopAccessibility.
#
# This is the ONLY file that registers @script decorators / gestures. The
# two backend modules (_hermes.py and _opencode.py) are plain Python
# classes; they don't know about NVDA's gesture system.
#
# Hotkey set (agent-aware, with global Codex transcript-buffer access):
#
#   Shared (Hermes, OpenCode, or ChatGPT Codex):
#     kb:NVDA+alt+downArrow   next message
#     kb:NVDA+alt+upArrow     previous message
#     kb:NVDA+alt+rightArrow  next active task (ChatGPT Codex)
#     kb:NVDA+alt+leftArrow   previous active task (ChatGPT Codex)
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
#   ChatGPT Codex only:
#     kb:NVDA+alt+enter       send a message through consumer Chat and auto-read the response
#     kb:NVDA+alt+c           accessible application menus
#     kb:NVDA+alt+p           project and task picker
#     kb:NVDA+alt+t           active task picker
#     kb:NVDA+alt+u           usage summary; press twice for confirmed banked reset (available globally)
#     kb:NVDA+alt+space       re-read current transcript message
#
#   OpenCode and ChatGPT Codex:
#     kb:NVDA+alt+t           read thinking trace (active tasks in ChatGPT)
#     kb:NVDA+alt+a           toggle auto-read
#
#   Hermes only — PRESERVED from the original hermesAccessibility 1.7.2
#   (pass-through when OpenCode is foreground):
#     kb:NVDA+alt+space       @ reference picker
#     kb:NVDA+shift+h         toggle speech filter
#     kb:NVDA+shift+j         speech filter status
#
#   Hermes only — self-healing Hermes app.asar patcher is invoked
#   once on load (_warmPatcher). The OpenCode asar patcher was
#   REMOVED in 2.4.0 because it destabilized the OpenCode renderer.
#
# Removed in this release (the user asked for the OpenCode arrow/Home/End
# set to replace them):
#   NVDA+Alt+N/P/L/C/R  (Hermes message-nav letters)
#   NVDA+Alt+I          (Hermes position info)
#   NVDA+Alt+S in the Hermes sense still works — the dispatcher routes it
#   to the Hermes session picker when Hermes is foreground, the OpenCode
#   session switcher when OpenCode is foreground.
#
# Pass-through: unrelated foreground-specific gestures still call
# gesture.send(). Codex message navigation and left/right task cycling are
# intentionally global and operate on NVDA's transcript buffer in the background.

import globalPluginHandler
import ui
from scriptHandler import getLastScriptRepeatCount, script
from logHandler import log

from .addtl.router import route, route_message_command
from .addtl.hermesBackend import HermesBackend
from .addtl.opencodeBackend import OpenCodeBackend
from .addtl.chatgptBackend import ChatGPTBackend


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    scriptCategory = "Agent Desktop Accessibility"

    def __init__(self):
        super().__init__()
        log.info("agentDesktopAccessibility 2.8.8 loading")
        self._hermes = HermesBackend()
        self._opencode = OpenCodeBackend(plugin=self)
        self._chatgpt = ChatGPTBackend()
        log.info("agentDesktopAccessibility 2.8.8 loaded — backends: hermes, opencode, chatgpt")
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
                self._hermes.ensurePatcher(verbose=True)
        except Exception as e:
            try:
                from logHandler import log
                log.warning("agentDesktopAccessibility: warmPatcher hermes error: %s", e)
            except Exception:
                pass
        # NOTE: The OpenCode asar self-heal block was removed in 2.4.0.
        # Patching OpenCode's app.asar destabilized the renderer (see
        # opencodeBackend._openProjectAndFocusSession for the full
        # history). The session picker now falls back to the native
        # `opencode://open-project?directory=...` deep link, which
        # OpenCode supports out of the box.

    def terminate(self):
        try:
            self._hermes.terminate()
        except Exception as e:
            log.warning("agentDesktopAccessibility: hermes terminate error: %s", e)
        try:
            self._opencode.terminate()
        except Exception as e:
            log.warning("agentDesktopAccessibility: opencode terminate error: %s", e)
        try:
            self._chatgpt.terminate()
        except Exception as e:
            log.warning("agentDesktopAccessibility: chatgpt terminate error: %s", e)
        super().terminate()

    # ─────────────────────────────────────────────────────────────
    # Shared message navigation — Hermes OR OpenCode
    # ─────────────────────────────────────────────────────────────

    @script(gesture="kb:NVDA+alt+downArrow",
            description="Next message (Hermes, OpenCode, or ChatGPT Codex)")
    def script_nextMessage(self, gesture):
        target = route_message_command()
        if target == "hermes":
            self._hermes.nextMessage()
        elif target == "opencode":
            self._opencode.nextMessage()
        elif target == "chatgpt":
            self._chatgpt.nextMessage()

    @script(gesture="kb:NVDA+alt+upArrow",
            description="Previous message (Hermes, OpenCode, or ChatGPT Codex)")
    def script_previousMessage(self, gesture):
        target = route_message_command()
        if target == "hermes":
            self._hermes.prevMessage()
        elif target == "opencode":
            self._opencode.previousMessage()
        elif target == "chatgpt":
            self._chatgpt.previousMessage()

    @script(gesture="kb:NVDA+alt+rightArrow",
            description="Next active task (ChatGPT Codex)")
    def script_nextActiveTask(self, gesture):
        self._chatgpt.nextSession()

    @script(gesture="kb:NVDA+alt+leftArrow",
            description="Previous active task (ChatGPT Codex)")
    def script_previousActiveTask(self, gesture):
        self._chatgpt.previousSession()

    @script(gesture="kb:NVDA+alt+home",
            description="First message (Hermes, OpenCode, or ChatGPT Codex)")
    def script_firstMessage(self, gesture):
        target = route_message_command()
        if target == "hermes":
            self._hermes.firstMessage()
        elif target == "opencode":
            self._opencode.firstMessage()
        elif target == "chatgpt":
            self._chatgpt.firstMessage()

    @script(gesture="kb:NVDA+alt+end",
            description="Last message (Hermes, OpenCode, or ChatGPT Codex)")
    def script_lastMessage(self, gesture):
        target = route_message_command()
        if target == "hermes":
            self._hermes.lastMessage()
        elif target == "opencode":
            self._opencode.lastMessage()
        elif target == "chatgpt":
            self._chatgpt.lastMessage()

    @script(gesture="kb:NVDA+alt+r",
            description="Re-read current message (Hermes, OpenCode, or ChatGPT Codex)")
    def script_readCurrentMessage(self, gesture):
        target = route_message_command()
        if target == "hermes":
            self._hermes.readCurrentMessage()
        elif target == "opencode":
            self._opencode.readCurrentMessage()
        elif target == "chatgpt":
            self._chatgpt.readCurrentMessage()

    # ─────────────────────────────────────────────────────────────
    # Session management — Hermes OR OpenCode
    # ─────────────────────────────────────────────────────────────

    @script(gesture="kb:NVDA+alt+s",
            description="Open session switcher (Hermes, OpenCode, or ChatGPT Codex)")
    def script_sessionPicker(self, gesture):
        target = route()
        if target == "hermes":
            self._hermes.pickSession()
        elif target == "opencode":
            self._opencode.openSessionPicker()
        elif target == "chatgpt":
            self._chatgpt.openSessionPicker()
        else:
            self._chatgpt.openSessionPicker()

    @script(gesture="kb:NVDA+alt+shift+n",
            description="Next session (Hermes, OpenCode, or ChatGPT Codex)")
    def script_nextSession(self, gesture):
        target = route()
        if target == "hermes":
            self._hermes.nextSession()
        elif target == "opencode":
            self._opencode.nextSession()
        elif target == "chatgpt":
            self._chatgpt.nextSession()
        else:
            gesture.send()

    @script(gesture="kb:NVDA+alt+shift+p",
            description="Previous session (Hermes, OpenCode, or ChatGPT Codex)")
    def script_previousSession(self, gesture):
        target = route()
        if target == "hermes":
            self._hermes.prevSession()
        elif target == "opencode":
            self._opencode.previousSession()
        elif target == "chatgpt":
            self._chatgpt.previousSession()
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
            description="Diagnostic dump (Hermes, OpenCode, or ChatGPT Codex)")
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
            # OpenCode no longer ships an asar patcher (removed in 2.4.0 —
            # see opencodeBackend._openProjectAndFocusSession for the
            # history). Nothing to audit here.
        elif target == "chatgpt":
            self._chatgpt.dumpDebug()
        else:
            gesture.send()

    # ─────────────────────────────────────────────────────────────
    # OpenCode-only — pass-through when Hermes is foreground
    # ─────────────────────────────────────────────────────────────

    @script(gesture="kb:NVDA+alt+t",
            description="Open active tasks in ChatGPT Codex or read thinking trace in OpenCode")
    def script_readThinking(self, gesture):
        target = route()
        if target == "opencode":
            self._opencode.readThinking()
        else:
            self._chatgpt.openTaskPicker()

    @script(gesture="kb:NVDA+alt+a",
            description="Toggle auto-read (OpenCode or ChatGPT Codex)")
    def script_toggleAutoRead(self, gesture):
        target = route()
        if target == "opencode":
            self._opencode.toggleAutoRead()
        elif target == "chatgpt":
            self._chatgpt.toggleAutoRead()
        else:
            gesture.send()

    # ─────────────────────────────────────────────────────────────
    # ChatGPT Codex-specific — preserved from Codex Accessibility 0.1.15
    # ─────────────────────────────────────────────────────────────

    @script(gesture="kb:NVDA+alt+enter",
            description="Send a message through ChatGPT Chat and auto-read the response")
    def script_sendChatMessage(self, gesture):
        if route() == "chatgpt":
            self._chatgpt.openChatPrompt()
        else:
            gesture.send()

    @script(gesture="kb:NVDA+alt+c",
            description="Open accessible ChatGPT application menus")
    def script_chatgptMenus(self, gesture):
        if route() == "chatgpt":
            self._chatgpt.openMenus()
        else:
            gesture.send()

    @script(gesture="kb:NVDA+alt+p",
            description="Open ChatGPT Codex project and task picker")
    def script_chatgptProjectPicker(self, gesture):
        self._chatgpt.openSessionPicker()

    @script(gesture="kb:NVDA+alt+u",
            description="Report ChatGPT Codex usage limits; press twice to use a banked reset (always available)")
    def script_chatgptUsageLimits(self, gesture):
        # The original Codex Accessibility add-on intentionally allowed
        # usage-limit reporting without requiring ChatGPT to be focused.
        self._chatgpt.handleUsageCommand(getLastScriptRepeatCount())

    # ─────────────────────────────────────────────────────────────
    # Hermes-only — PRESERVED from hermesAccessibility 1.7.2
    # Pass-through when OpenCode is foreground
    # ─────────────────────────────────────────────────────────────

    @script(gesture="kb:NVDA+alt+space",
            description="Hermes @ reference picker or current ChatGPT Codex message")
    def script_atRefPicker(self, gesture):
        target = route()
        if target == "hermes":
            self._hermes.showAtPicker()
        elif target == "chatgpt":
            self._chatgpt.readCurrentMessage()
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
        elif target == "chatgpt":
            title = self._opencode._detectForeground().get("title") or "ChatGPT"
            ui.message("Foreground: ChatGPT (Codex) — %s" % title)
        else:
            info = self._opencode._detectForeground()
            ui.message("Foreground: %s" % (info.get("title") or "(no title)"))
