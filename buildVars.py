# -*- coding: UTF-8 -*-
# buildVars.py - Build variables for agentDesktopAccessibility NVDA Addon
# See the file LICENSE for copying permission.
#
# Merged from hermesAccessibility 1.7.2 and opencodeAccessibility 1.1
# in 2026-07. See plan: C:/Users/willb/.hermes/plans/2026-07-03_232331-merge-hermes-opencode-addons.md

# Build variables for the addon
addon_info = {
	# Add-on information
	"addon_name": "agentDesktopAccessibility",
	"addon_summary": "Hermes + OpenCode Desktop Accessibility",
	"addon_description": "Merged add-on: message navigation, session switching, @-reference picker, and auto-read for the Hermes Agent and OpenCode Desktop apps. Hotkeys are foreground-aware; the same key does the right thing in each app. NVDA+Alt+Up/Down/Home/End navigate messages; NVDA+Alt+S opens a session picker; NVDA+Alt+Shift+D reports foreground metadata.",
	"addon_version": "2.1.0",
	"addon_author": "willb <willbilec@gmail.com>",
	"addon_url": "",
	"addon_docFileName": "readme.html",
	# Add-on update information
	"addon_updateChannel": None,
}

# Files that should be ignored when building the addon
excludedFiles = []

# The name of the manifest file
manifestFileName = "manifest.ini"
