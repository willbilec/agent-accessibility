# -*- coding: UTF-8 -*-
# appModules/Hermes.py
#
# Minimal app module for Hermes.exe.
#
# NVDA matches app modules by executable filename, so `Hermes.py`
# is loaded automatically when Hermes.exe runs.
#
# The actual speech suppression is handled by the hermesSpeechFilter
# global plugin, which hooks the synth driver directly — this is
# necessary because IA2 live region announcements in Electron apps
# bypass NVDA's speech.speak() pipeline entirely.
#
# Previous approaches removed because they don't work for IA2/Electron:
#   - chooseNVDAObjectOverlayClasses: only sees the document container,
#     not individual web elements inside it
#   - cancelSpeech() timer: destroys ALL speech including AI responses
#
# For message navigation, see the hermesMessageNav global plugin.

import appModuleHandler


class AppModule(appModuleHandler.AppModule):
    pass
