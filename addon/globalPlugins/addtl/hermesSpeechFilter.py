# -*- coding: UTF-8 -*-
# globalPlugins/hermesSpeechFilter.py
#
# Suppresses Hermes status speech by hooking the synth driver directly.
#
# WHY THIS APPROACH:
# In Electron/IA2 apps (like Hermes), live region announcements bypass
# NVDA's speech.speak() entirely — the IA2 handler speaks directly at
# the synth level. Monkey-patching speech.speak has NO effect.
# The synth driver's .speak() method is the lowest Python-level hook
# that can intercept IA2 live region speech before it reaches the synth.
#
# NOTE FOR agentDesktopAccessibility 2.0.0:
# This module is loaded as a backend by hermesBackend.py, NOT as a plugin
# by NVDA. The GlobalPlugin class below is kept for the state-holding
# pattern (so hermesBackend can call .toggle_filter() / .filter_status()
# on a stable instance), but its __gestures dict is intentionally empty
# because the dispatcher in agentDesktopAccessibility.py owns all bindings.

import globalPluginHandler
import api
import ui
import speech
import synthDriverHandler
import re
import time


# ── Status text patterns ─────────────────────────────────────────────

_RE_STATUS = re.compile(
    r'^[\u2800-\u28FF\s]+$|'            # Braille spinner chars (U+2800-U+28FF)
    r'^\d{1,3}:\d{2}s?$|'               # Timer: "1:13", "10:30", "2:45s"
    r'^\d{1,3}m\s*\d{1,2}s$|'           # Timer: "1m 13s", "5m 30s"
    r'^\d{1,3}s$|'                       # Timer: "1s", "2s", ..., "999s"
    # Single status words with optional trailing dots
    r'^(thinking|running|done|ready|idle|loading|processing|'
    r'waiting|writing|working|working\.\.\.)\s*\.*$|'
    # "Hermes is ..." phrases (with or without trailing text)
    r'^hermes\s+(is\s+)?(loading|thinking|running|done|processing|'
    r'working|writing|responding).*$',
    re.IGNORECASE
)


def _isStatusText(text):
    """Return True if text matches known Hermes status spam patterns."""
    if not text:
        return False
    stripped = text.strip()
    if not stripped:
        return False
    return bool(_RE_STATUS.match(stripped))


# ── Shared state ─────────────────────────────────────────────────────

class _State:
    synthHooked = False
    origSynthSpeak = None
    suppressCount = 0
    enabled = True
    # Foreground cache to avoid hitting api.getForegroundObject()
    # on every single synth.speak() call (which can fire rapidly)
    _lastFgCheck = 0
    _lastFgResult = False


_state = _State()


# ── Foreground detection (cached) ────────────────────────────────────

def _isHermesForeground():
    """Check if a Hermes window is in the foreground (cached 250ms)."""
    now = time.time()
    if now - _state._lastFgCheck < 0.25:
        return _state._lastFgResult
    _state._lastFgCheck = now
    try:
        fg = api.getForegroundObject()
        if fg is not None:
            appMod = getattr(fg, 'appModule', None)
            if appMod is not None:
                name = getattr(appMod, 'appName', '') or ''
                _state._lastFgResult = 'hermes' in name.lower()
                return _state._lastFgResult
    except Exception:
        pass
    _state._lastFgResult = False
    return False


# ── Synth driver hook ────────────────────────────────────────────────

def _createSynthWrapper():
    """Create and return a wrapper function for synth.speak()."""
    _orig = _state.origSynthSpeak

    def _synthWrapper(speechSequence):
        # Pass through if disabled
        if not _state.enabled:
            return _orig(speechSequence)

        # Only filter when Hermes is the foreground app
        if not _isHermesForeground():
            return _orig(speechSequence)

        # Extract text items from the speech sequence
        text_items = [
            (i, item) for i, item in enumerate(speechSequence)
            if isinstance(item, str)
        ]

        if not text_items:
            # No text at all (just SpeechCommands) — pass through
            return _orig(speechSequence)

        # Check if ALL text is status spam
        all_status = all(_isStatusText(t) for _, t in text_items)

        if all_status:
            _state.suppressCount += 1
            return  # Suppress entirely — don't call the synth

        # Mixed content: filter out status text strings but keep
        # SpeechCommands and non-status text
        filtered = []
        for item in speechSequence:
            if isinstance(item, str) and _isStatusText(item):
                continue
            filtered.append(item)

        # If no text remains after filtering, suppress
        has_text = any(isinstance(item, str) for item in filtered)
        if not has_text:
            _state.suppressCount += 1
            return

        return _orig(filtered)

    return _synthWrapper


def _installHook():
    """Install the synth driver speak hook. Returns True on success."""
    if _state.synthHooked:
        # Verify our hook is still in place (synth may have changed)
        try:
            synth = synthDriverHandler.getSynth()
            if synth is not None and synth.speak is not _state._synthWrapper:
                # Hook was lost — re-install with fresh wrapper
                _state.origSynthSpeak = synth.speak
                _state._synthWrapper = _createSynthWrapper()
                synth.speak = _state._synthWrapper
        except Exception:
            pass
        return True

    try:
        synth = synthDriverHandler.getSynth()
        if synth is None:
            return False

        _state.origSynthSpeak = synth.speak
        _state._synthWrapper = _createSynthWrapper()
        synth.speak = _state._synthWrapper
        _state.synthHooked = True
        return True
    except Exception:
        return False


def _uninstallHook():
    """Remove the synth driver speak hook."""
    if not _state.synthHooked:
        return
    try:
        synth = synthDriverHandler.getSynth()
        if synth is not None and _state.origSynthSpeak is not None:
            # Only restore if our wrapper is still in place
            if synth.speak is _state._synthWrapper:
                synth.speak = _state.origSynthSpeak
    except Exception:
        pass
    _state.synthHooked = False
    _state.origSynthSpeak = None


# ── Global plugin ────────────────────────────────────────────────────

class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    """Hermes speech filter — natively suppresses status announcements."""

    scriptCategory = "Hermes Accessibility"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._timer = None
        self._retryCount = 0

        # Try to install the hook immediately
        if not _installHook():
            # Synth not ready yet (NVDA still starting) — retry on timer
            self._startRetryTimer()
        else:
            # Hook installed; start maintenance timer to detect synth changes
            self._startMaintenanceTimer()

    def terminate(self):
        self._stopTimer()
        _uninstallHook()
        super().terminate()

    # ── Timer management ──────────────────────────────────────────

    def _startRetryTimer(self):
        """Retry hook installation every second (synth not ready yet)."""
        try:
            import wx
            if self._timer is not None:
                return
            self._timer = wx.Timer()
            self._timer.notify = self._onRetryTimer
            self._timer.Start(1000)
        except Exception:
            pass

    def _startMaintenanceTimer(self):
        """Periodically verify the hook is still in place."""
        try:
            import wx
            if self._timer is not None:
                self._timer.Stop()
            self._timer = wx.Timer()
            self._timer.notify = self._onMaintenanceTimer
            self._timer.Start(5000)  # every 5 seconds
        except Exception:
            pass

    def _stopTimer(self):
        if self._timer is not None:
            try:
                self._timer.Stop()
                self._timer = None
            except Exception:
                pass

    def _onRetryTimer(self, event=None):
        if _state.synthHooked:
            self._startMaintenanceTimer()
            return
        self._retryCount += 1
        if self._retryCount > 30:
            self._stopTimer()
            return
        if _installHook():
            self._startMaintenanceTimer()

    def _onMaintenanceTimer(self, event=None):
        """Verify hook integrity and re-install if synth changed."""
        if not _state.synthHooked:
            _installHook()
            return
        # Verify hook is still in place
        try:
            synth = synthDriverHandler.getSynth()
            if synth is not None:
                if synth.speak is not _state._synthWrapper:
                    # Synth changed (user switched synths) — re-hook
                    _state.origSynthSpeak = synth.speak
                    _state._synthWrapper = _createSynthWrapper()
                    synth.speak = _state._synthWrapper
        except Exception:
            pass

    # ── Gestures ──────────────────────────────────────────────────

    def toggle_filter(self):
        _state.enabled = not _state.enabled
        status = "enabled" if _state.enabled else "disabled"
        active = " (hermes active)" if _isHermesForeground() else ""
        ui.message(
            f"Hermes speech filter {status}{active}. "
            f"Suppressed {_state.suppressCount} utterances so far."
        )

    def filter_status(self):
        hooked = "hooked" if _state.synthHooked else "NOT hooked"
        active = "hermes foreground" if _isHermesForeground() else "hermes not active"
        enabled = "on" if _state.enabled else "off"
        ui.message(
            f"Hermes filter: synth={hooked}, {active}, toggle={enabled}, "
            f"suppressed={_state.suppressCount}"
        )

    # NB: __gestures was removed in agentDesktopAccessibility 2.0.0. The
    # dispatcher (agentDesktopAccessibility.py) binds NVDA+Shift+H/J on
    # its own script_* wrappers and routes to hermesBackend.toggleFilter()
    # / .filterStatus(), which in turn call self.toggle_filter() /
    # self.filter_status() on a single HermesBackend-owned instance of
    # this class. If you re-add __gestures here, NVDA will double-bind the
    # keys (this instance's methods + the dispatcher's wrappers) and the
    # user will see no behaviour change but you'll leak synth-hook calls.
