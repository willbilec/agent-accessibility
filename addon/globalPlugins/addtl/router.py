# -*- coding: UTF-8 -*-
# globalPlugins/router.py
#
# Foreground-aware dispatcher helper for agentDesktopAccessibility.
#
# The dispatcher in agentDesktopAccessibility.py calls route() to decide
# which backend (Hermes or OpenCode) should handle a given keystroke.
#
# IMPORTANT: this file is intentionally NOT a plugin (no GlobalPlugin class).
# It exists purely as a helper module that the dispatcher imports. NVDA's
# plugin loader will try to register it as a plugin and log an
# AttributeError on every startup; that's noise, not breakage.
#
# Returns 'hermes', 'opencode', or None (neither app is foreground).
#
# Caches the result for 250ms to avoid hammering the UI Automation
# tree on every keystroke.

import time
import api

_CACHE_TTL = 0.25  # seconds

_last_check = 0.0
_last_hermes = False
_last_opencode = False


def _now():
    return time.monotonic()


def reset_cache():
    """Clear the foreground cache. Useful for tests or when an event hook
    definitively knows the foreground changed."""
    global _last_check, _last_hermes, _last_opencode
    _last_check = 0.0
    _last_hermes = False
    _last_opencode = False


def is_hermes():
    """True if the foreground app is Hermes (Electron desktop)."""
    global _last_check, _last_hermes
    now = _now()
    if now - _last_check < _CACHE_TTL and _last_check > 0:
        return _last_hermes
    _refresh()
    return _last_hermes


def is_opencode():
    """True if the foreground app is OpenCode Desktop.

    This uses the same detection logic as the original opencodeAccessibility
    plugin's _isOpenCode() — checking title, className, accName, appName,
    productName, and processPath. A naive title/appName check is NOT
    enough: the OpenCode Electron app's appModule name can vary, and
    the only reliable signal is the full set of fields. The original
    _isOpenCode worked; we mirror its logic here so the dispatcher
    routes to the OpenCode backend correctly."""
    global _last_check, _last_opencode
    now = _now()
    if now - _last_check < _CACHE_TTL and _last_check > 0:
        return _last_opencode
    _refresh()
    return _last_opencode


def route():
    """Return 'hermes', 'opencode', or None."""
    if is_hermes():
        return 'hermes'
    if is_opencode():
        return 'opencode'
    return None


def _refresh():
    """Recompute both flags from the current foreground object."""
    global _last_check, _last_hermes, _last_opencode
    _last_check = _now()
    _last_hermes = False
    _last_opencode = False
    try:
        fg = api.getForegroundObject()
    except Exception:
        fg = None
    if fg is None:
        return

    # Hermes detection: appModule.appName contains "hermes".
    try:
        title = (getattr(fg, 'name', '') or '').lower()
    except Exception:
        title = ''
    am = getattr(fg, 'appModule', None)
    app_name = ''
    product_name = ''
    if am is not None:
        try:
            app_name = (getattr(am, 'appName', '') or '').lower()
        except Exception:
            app_name = ''
        try:
            product_name = (getattr(am, 'productName', '') or '').lower()
        except Exception:
            product_name = ''

    if 'hermes' in app_name:
        _last_hermes = True
        return  # mutually exclusive

    # OpenCode detection: mirror the original opencodeAccessibility
    # _isOpenCode() heuristic. We check more fields than just title/appName
    # because the OpenCode Electron app doesn't always populate appName
    # cleanly — processPath is the most reliable signal.
    class_name = ''
    acc_name = ''
    process_path = ''
    if am is not None:
        try:
            class_name = (getattr(am, 'windowClassName', '') or '').lower()
        except Exception:
            class_name = ''
        # accName lives on the foreground object, not the appModule
        try:
            acc_name = (getattr(fg, 'description', '') or '').lower()
        except Exception:
            acc_name = ''
    # processPath is the most reliable signal but is expensive (ctypes
    # call to get the module path). Look it up via the appModule if it
    # exposes one (NVDA's appModule doesn't always), otherwise fall back
    # to the cheaper checks.
    if am is not None:
        try:
            process_path = (getattr(am, 'processPath', '') or '').lower()
        except Exception:
            process_path = ''

    fields = (title, class_name, acc_name, app_name, product_name, process_path)
    for needle in ('opencode', 'open code', 'opencode-desktop'):
        for field in fields:
            if field and needle in field:
                _last_opencode = True
                return

    # If appName/productName is empty (the OpenCode Electron app's
    # appModule sometimes reports blank), try the process executable name
    # via the title as a last resort.
    if title and 'opencode' in title:
        _last_opencode = True
        return
