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
	"addon_summary": "Hermes + OpenCode + ChatGPT Codex Accessibility",
	"addon_description": "Merged NVDA accessibility for Hermes Agent, OpenCode Desktop, and the ChatGPT desktop app. Includes global Codex auto-read of collapsed activity summaries, commentary, and final responses from every active top-level task, using stable per-task offsets and separate NVDA-priority utterances; completion notifications; global Codex transcript reading; background-only NVDA buffer task cycling and foreground live-task-synchronized navigation limited to running or seven-day-recent tasks; globally available Codex project/task pickers that restore an existing ChatGPT window or cold-start it before applying the selection; consumer Chat message sending with spoken assistant activity updates; foreground-aware Hermes/OpenCode routing; Codex menu and project/task mirrors; usage limits with confirmed banked-reset redemption; the Hermes reference picker and speech filter; and OpenCode auto-read.",
	"addon_version": "2.8.8",
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
