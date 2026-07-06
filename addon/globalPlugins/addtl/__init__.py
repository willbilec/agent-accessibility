# -*- coding: UTF-8 -*-
# globalPlugins/addtl/__init__.py
#
# Sub-package for the agentDesktopAccessibility add-on's helper modules.
# NVDA's plugin loader scans this sub-package looking for a `GlobalPlugin`
# class — it logs `AttributeError` if none exists. We provide a no-op stub
# so the loader is satisfied without instantiating anything.
#
# The real plugin (and the only one that owns gestures) lives at
# `globalPlugins/agentDesktopAccessibility.py`.

import globalPluginHandler


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    """Stub plugin class.

    NVDA's loader instantiates one of these at startup just to confirm the
    sub-package is a valid plugin module. It has no __gestures dict and no
    script methods — every keystroke is handled by the dispatcher in
    `globalPlugins/agentDesktopAccessibility.py` instead.
    """
    scriptCategory = "Agent Desktop Accessibility (helpers — should never see this category)"
