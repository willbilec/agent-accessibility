# -*- coding: UTF-8 -*-
# globalPlugins/addtl/freebuffBackend.py
#
# Freebuff Desktop backend for agentDesktopAccessibility.
#
# Freebuff stores one SQLite database per project under
# ~/.config/freebuff-desktop/projects/<name>-<projectId>/desktop-v2.db and
# names the active thread in ~/.config/freebuff-desktop/state.json
# (workspace.activeId). NVDA's embedded Python has no sqlite3, so the reads
# go through freebuffStore.py under the system Python — the same arrangement
# the OpenCode backend uses.
#
# Freebuff registers no URL scheme at all, so thread switching is a
# best-effort activation of the matching object in the renderer's
# accessibility tree (see TranscriptBackend._activateByTitle).
#
# Python discovery and the helper can take seconds (a slow interpreter start,
# a locked or large store), so they run on a worker thread. Gestures read a
# cached snapshot; a stale one is refreshed in the background and the gesture
# is replayed on NVDA's main thread once the read finishes.

import glob
import json
import os
import subprocess
import threading
import time

import ui
import wx
from logHandler import log

from .transcriptBackend import _MESSAGES_TTL, _THREADS_TTL, TranscriptBackend

_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "freebuff-desktop")
_HELPER_TIMEOUT_S = 15


class FreebuffBackend(TranscriptBackend):

	appLabel = "Freebuff"
	threadNoun = "thread"
	logPrefix = "freebuffAccessibility"

	def __init__(self, plugin=None):
		super().__init__(plugin)
		self._pythonExe = None
		# What the last background read returned; threads is None until the
		# list has been read once.
		self._snapshotMessages = []
		self._snapshotThreadId = ""
		self._snapshotThreads = None
		self._messagesReadAt = 0.0
		self._threadsReadAt = 0.0
		self._busy = False
		log.info("Freebuff backend loaded")

	# ------------------------------------------------------------------
	# System Python discovery (cached)
	# ------------------------------------------------------------------

	def _getPythonExe(self):
		if self._pythonExe:
			return self._pythonExe
		candidates = ["python", "python3", "py"]
		for pattern in (
			os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python", "Python3*", "python.exe"),
			os.path.join(os.path.expanduser("~"), "AppData", "Local", "Programs", "Python", "Python3*", "python.exe"),
		):
			candidates.extend(sorted(glob.glob(pattern), reverse=True))
		for exe in candidates:
			if not exe:
				continue
			test_cmd = [exe, "-c", "import sqlite3"]
			if exe == "py":
				test_cmd = ["py", "-3", "-c", "import sqlite3"]
			try:
				proc = subprocess.run(
					test_cmd,
					capture_output=True,
					timeout=10,
					creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
				)
			except Exception:
				continue
			if proc.returncode == 0:
				self._pythonExe = exe
				self._dbg("python: using", exe)
				return exe
		self._dbg("python: none found")
		return None

	# ------------------------------------------------------------------
	# freebuffStore.py
	# ------------------------------------------------------------------

	def _runHelper(self, list_mode=False):
		helper = os.path.join(os.path.dirname(__file__), "freebuffStore.py")
		if not os.path.isfile(helper):
			self._dbg("helper missing:", helper)
			return {}
		python_exe = self._getPythonExe()
		if not python_exe:
			return {}
		cmd = [python_exe, helper, _CONFIG_DIR]
		if list_mode:
			cmd.append("--list")
		try:
			proc = subprocess.run(
				cmd,
				capture_output=True,
				encoding="utf-8",
				timeout=_HELPER_TIMEOUT_S,
				creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
			)
		except Exception as e:
			self._dbg("helper error:", e)
			return {}
		if proc.returncode != 0:
			self._dbg("helper exit", proc.returncode, (proc.stderr or "")[:300])
			return {}
		try:
			return json.loads((proc.stdout or "").strip() or "{}")
		except ValueError as e:
			self._dbg("helper json error:", e)
			return {}

	# ------------------------------------------------------------------
	# Background reads (worker thread only)
	# ------------------------------------------------------------------

	def _readMessages(self):
		if not os.path.isdir(_CONFIG_DIR):
			self._dbg("config dir missing:", _CONFIG_DIR)
			return [], ""
		data = self._runHelper()
		messages = data.get("messages") or []
		thread_id = data.get("thread_id") or ""
		self._dbg("loadMessages: thread=%s messages=%d" % (thread_id, len(messages)))
		return messages, thread_id

	def _readThreads(self):
		data = self._runHelper(list_mode=True)
		threads = []
		for thread in data.get("threads") or []:
			project_path = (thread.get("projectPath") or "").rstrip("/\\")
			threads.append({
				"id": thread.get("id") or "",
				"title": (thread.get("title") or "").strip(),
				# basename() is empty for a drive root like "C:", so fall back
				# to the path itself rather than labelling the thread "()".
				"detail": (os.path.basename(project_path) or project_path) if project_path else "",
				"sort": thread.get("updatedAt") or 0,
			})
		threads.sort(key=lambda thread: thread["sort"], reverse=True)
		return threads

	def _request(self, action, force=False):
		"""Run a base navigation action, reading the store off-thread first if needed."""
		if not self._running:
			return
		listing = action in ("openSessionPicker", "nextSession", "previousSession")
		now = time.monotonic()
		if listing:
			fresh = (self._snapshotThreads is not None and action != "openSessionPicker"
				and now - self._threadsReadAt < _THREADS_TTL)
		else:
			fresh = (not force and self._messagesReadAt
				and now - self._messagesReadAt < _MESSAGES_TTL)
		if fresh:
			getattr(super(), action)()
			return
		if self._busy:
			ui.message("Freebuff is still loading")
			return
		self._busy = True
		if not self._messagesReadAt and self._snapshotThreads is None:
			# Only the first read is slow enough to need saying: it may have to
			# find a Python with sqlite3 before the store can be opened.
			ui.message("Loading Freebuff")

		def read():
			result, error = None, None
			try:
				result = self._readThreads() if listing else self._readMessages()
			except Exception as e:
				error = e
			wx.CallAfter(self._finish, action, listing, result, error)

		threading.Thread(target=read, daemon=True).start()

	def _finish(self, action, listing, result, error):
		self._busy = False
		if not self._running:
			return
		if error is not None:
			log.warning("Freebuff: could not read the store: %s", error)
			self._dbg("read error:", error)
		elif listing:
			self._snapshotThreads = result
			self._threadsReadAt = time.monotonic()
		else:
			self._snapshotMessages, self._snapshotThreadId = result
			self._messagesReadAt = time.monotonic()
		getattr(super(), action)()

	# ------------------------------------------------------------------
	# TranscriptBackend hooks: served from the snapshot, never blocking
	# ------------------------------------------------------------------

	def loadMessages(self):
		return self._snapshotMessages, self._snapshotThreadId

	def loadThreads(self):
		return self._snapshotThreads or []

	def _invalidateMessages(self):
		super()._invalidateMessages()
		self._messagesReadAt = 0.0

	def nextMessage(self):
		self._request("nextMessage")

	def previousMessage(self):
		self._request("previousMessage")

	def firstMessage(self):
		self._request("firstMessage", force=True)

	def lastMessage(self):
		self._request("lastMessage", force=True)

	def readCurrentMessage(self):
		self._request("readCurrentMessage")

	def readThinking(self):
		self._request("readThinking")

	def openSessionPicker(self):
		self._request("openSessionPicker")

	def nextSession(self):
		self._request("nextSession")

	def previousSession(self):
		self._request("previousSession")

	def dumpDebug(self):
		self._request("dumpDebug", force=True)
