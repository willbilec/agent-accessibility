# Copyright (C) 2026 Will Bishop
# This file is covered by the GNU General Public License.

"""ChatGPT desktop backend for the Codex accessibility helpers.

The desktop application was renamed from Codex to ChatGPT in 2026.  Its
package, local data, CLI, and deep-link protocol still use the Codex name, so
this module deliberately keeps ``codex`` for those integration surfaces while
detecting and launching the current ``ChatGPT.exe`` desktop process.

This module is a backend, not a separately loaded NVDA global plugin.  Gesture
registration lives in ``agentDesktopAccessibility.py`` so the same shortcuts
can be routed safely between Hermes, OpenCode, and ChatGPT.
"""

from __future__ import annotations

from datetime import datetime
import glob
import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import uuid
from urllib.parse import quote
import ctypes

import api
import keyboardHandler
import speech
import ui
import winUser
import wx
from logHandler import log

try:
	import sqlite3
except ImportError:
	# NVDA 2026.1's embedded Python omits the optional SQLite extension. Task
	# history can fall back to Codex's JSONL session index without preventing
	# the entire global plug-in (and all of its gestures) from loading.
	sqlite3 = None

try:
	import uiautomation
except ImportError:
	uiautomation = None

try:
	import gui
except ImportError:
	gui = None


# ChatGPT.exe is the current desktop renderer.  codex.exe is retained for
# compatibility with older Codex desktop builds; it is also the CLI name, but
# a CLI process cannot become NVDA's foreground object.
CHATGPT_PROCESS_NAMES = {"chatgpt.exe", "codex.exe"}
CODEX_PROCESS_NAMES = CHATGPT_PROCESS_NAMES
CODEX_HOME = os.path.join(os.path.expanduser("~"), ".codex")
SESSIONS_DIR = os.path.join(CODEX_HOME, "sessions")
GLOBAL_STATE_PATH = os.path.join(CODEX_HOME, ".codex-global-state.json")
SESSION_INDEX_PATH = os.path.join(CODEX_HOME, "session_index.jsonl")
CODEX_EXECUTABLE_CACHE_PATH = os.path.join(CODEX_HOME, ".codex-executable-path.txt")
PROJECT_PICKER_STATE_PATH = os.path.join(CODEX_HOME, "codexAccessibility-project-picker-state.json")
STATE_DB_PATTERN = os.path.join(CODEX_HOME, "state_*.sqlite")
APP_SERVER_TIMEOUT_SECONDS = 10.0
CHAT_COMPOSER_NAME = "Message ChatGPT"
CHAT_SEND_BUTTON_NAMES = ("Send", "Send message")
CHAT_RESPONSE_STATUS_NAMES = ("ChatGPT is responding",)
CHAT_RESPONSE_POLL_MS = 1500
CHAT_RESPONSE_TIMEOUT_SECONDS = 180.0
CODEX_AUTO_READ_POLL_MS = 1000
CODEX_AUTO_READ_MAX_CHARS = 4000
CODEX_ACTIVE_TASK_REFRESH_SECONDS = 10.0
CODEX_TASK_CYCLE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
# Consumer Chat exposes short progress cards such as "Searching the web" and
# "Thought for 12s" separately from the assistant answer.  They are usually
# static text rather than a live region, so NVDA does not announce them.
CHAT_RESPONSE_ACTIVITY_RE = re.compile(
	r"^(?:"
	r"thought\s+for\s+.+|"
	r"(?:thinking|working|searching|browsing|researching|reading|"
	r"analy[sz]ing|planning|reasoning|gathering|checking|looking\s+up|"
	r"writing|implementing|reviewing|inspecting|exploring|investigating|"
	r"running|testing|updating|editing|fixing|creating|building|installing|"
	r"executing|preparing|drafting|summarizing|searched|browsed|researched|"
	r"read|analy[sz]ed|used)"
	r"(?:\s|$|[.:…])"
	r")",
	re.IGNORECASE,
)
FIVE_HOUR_WINDOW_MINS = 300
WEEKLY_WINDOW_MINS = 10080
_SYSTEM_PYTHON_COMMAND = None
_SYSTEM_PYTHON_PROBED = False


class _AutoReadTimer(wx.Timer):
	"""wx timer whose real virtual callback drives the Codex monitor."""

	def __init__(self, callback):
		super(_AutoReadTimer, self).__init__()
		self._callback = callback

	def Notify(self):
		# wxPython calls the capitalized virtual method. Assigning an instance
		# attribute named ``notify`` does not register a timer callback.
		self._callback()


MENU_SECTIONS = (
	("File", (("New task", "control+n"), ("New window", "control+shift+n"), ("Close window", "alt+f4"))),
	("Edit", (("Undo", "control+z"), ("Redo", "control+y"), ("Cut", "control+x"), ("Copy", "control+c"), ("Paste", "control+v"), ("Select all", "control+a"))),
	("View", (("Reload", "control+r"), ("Zoom in", "control+="), ("Zoom out", "control+-"), ("Reset zoom", "control+0"), ("Toggle developer tools", "control+shift+i"))),
	("Window", (("Minimize", "alt+space,n"), ("Close", "alt+f4"))),
	("Help", (("No reliable Codex Help menu shortcuts are known yet", None),)),
)


def _codexCliCommand():
	vendorRoot = os.path.join(
		os.path.expanduser("~"),
		"AppData",
		"Roaming",
		"npm",
		"node_modules",
		"@openai",
		"codex",
		"node_modules",
		"@openai",
		"codex-win32-x64",
		"vendor",
		"x86_64-pc-windows-msvc",
	)
	# Codex 0.144 moved the native executable from codex/codex.exe to
	# bin/codex.exe. Keep the older location for compatible installations.
	for relativePath in (("bin", "codex.exe"), ("codex", "codex.exe")):
		vendorExe = os.path.join(vendorRoot, *relativePath)
		if os.path.isfile(vendorExe):
			return [vendorExe]
	npmCodex = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "npm", "codex.cmd")
	if os.path.isfile(npmCodex):
		return [npmCodex]
	return ["codex"]


def _subprocessCreationFlags():
	return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _subprocessStartupInfo():
	startupInfoFactory = getattr(subprocess, "STARTUPINFO", None)
	useShowWindow = getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
	showWindowHidden = getattr(subprocess, "SW_HIDE", 0)
	if startupInfoFactory is None:
		return None
	startupInfo = startupInfoFactory()
	startupInfo.dwFlags |= useShowWindow
	startupInfo.wShowWindow = showWindowHidden
	return startupInfo


def _isChatGPTForeground():
	try:
		appPath = api.getForegroundObject().appModule.appPath
	except Exception:
		return False
	return os.path.basename(appPath or "").lower() in CHATGPT_PROCESS_NAMES


# Compatibility name used throughout the original Codex add-on and by its
# regression tests.
def _isCodexForeground():
	return _isChatGPTForeground()


def _latestSessionPath():
	if not os.path.isdir(SESSIONS_DIR):
		return None
	latestPath = None
	latestMtime = -1.0
	for root, _dirs, files in os.walk(SESSIONS_DIR):
		for name in files:
			if not name.endswith(".jsonl"):
				continue
			path = os.path.join(root, name)
			try:
				mtime = os.path.getmtime(path)
			except OSError:
				continue
			if mtime > latestMtime:
				latestPath = path
				latestMtime = mtime
	return latestPath


def _sessionPaths():
	if not os.path.isdir(SESSIONS_DIR):
		return []
	paths = []
	for root, _dirs, files in os.walk(SESSIONS_DIR):
		for name in files:
			if name.endswith(".jsonl"):
				paths.append(os.path.join(root, name))
	return paths


def _contentText(content):
	parts = []
	for item in content or []:
		if not isinstance(item, dict):
			continue
		text = item.get("text")
		if isinstance(text, str) and text.strip():
			parts.append(text.strip())
	return "\n\n".join(parts).strip()


def _sessionMetadata(path):
	if not path:
		return {}
	try:
		with open(path, "r", encoding="utf-8", errors="replace") as sessionFile:
			firstLine = sessionFile.readline()
	except OSError:
		return {}
	try:
		record = json.loads(firstLine)
	except ValueError:
		return {}
	if record.get("type") != "session_meta" or not isinstance(record.get("payload"), dict):
		return {}
	return record["payload"]


def _sessionPathForId(sessionId):
	if not isinstance(sessionId, str) or not sessionId.strip() or not os.path.isdir(SESSIONS_DIR):
		return None
	filenameSuffix = "-%s.jsonl" % sessionId
	for root, _dirs, files in os.walk(SESSIONS_DIR):
		for name in files:
			if name.endswith(filenameSuffix):
				return os.path.join(root, name)
	# Older or nonstandard Codex builds may use a different filename. Fall
	# back to the authoritative id in the first session_meta record.
	for root, _dirs, files in os.walk(SESSIONS_DIR):
		for name in files:
			if not name.endswith(".jsonl"):
				continue
			path = os.path.join(root, name)
			if _sessionMetadata(path).get("id") == sessionId:
				return path
	return None


def _loadTranscriptPath(path):
	if path is None:
		return None, []
	messages = []
	try:
		with open(path, "r", encoding="utf-8", errors="replace") as logFile:
			for line in logFile:
				try:
					record = json.loads(line)
				except ValueError:
					continue
				if record.get("type") != "response_item":
					continue
				payload = record.get("payload")
				if not isinstance(payload, dict) or payload.get("type") != "message":
					continue
				role = payload.get("role")
				if role not in ("user", "assistant", "system"):
					continue
				text = _contentText(payload.get("content"))
				if text:
					messages.append({"role": role, "text": text, "phase": payload.get("phase")})
	except OSError:
		return path, []
	return path, messages


def _reasoningSummaryText(payload):
	summaries = payload.get("summary") if isinstance(payload, dict) else None
	if not isinstance(summaries, list):
		return ""
	for item in reversed(summaries):
		if not isinstance(item, dict) or item.get("type") != "summary_text":
			continue
		text = item.get("text")
		if isinstance(text, str) and text.strip():
			# The UI renders Markdown emphasis without exposing the asterisks in
			# the collapsed button's accessible name. Match that spoken label.
			return text.strip().strip("*").strip()
	return ""


def _readTranscriptMessagesFromOffset(path, offset):
	"""Read complete messages and visible reasoning summaries after an offset."""
	if path is None:
		return [], 0
	try:
		size = os.path.getsize(path)
	except OSError:
		return [], 0
	if offset is None or offset > size:
		return [], size
	items = []
	try:
		with open(path, "r", encoding="utf-8", errors="replace") as logFile:
			logFile.seek(offset)
			while True:
				lineStart = logFile.tell()
				line = logFile.readline()
				if not line:
					break
				try:
					record = json.loads(line)
				except ValueError:
					# A writer can briefly expose an incomplete final line. Leave the
					# offset at its start so the next poll retries the complete record.
					if not line.endswith("\n"):
						logFile.seek(lineStart)
						break
					continue
				if record.get("type") != "response_item":
					continue
				payload = record.get("payload")
				if not isinstance(payload, dict):
					continue
				if payload.get("type") == "reasoning":
					text = _reasoningSummaryText(payload)
					if text:
						items.append({"role": "reasoning", "text": text, "phase": "reasoning"})
					continue
				if payload.get("type") != "message":
					continue
				role = payload.get("role")
				if role not in ("user", "assistant", "system"):
					continue
				text = _contentText(payload.get("content"))
				if text:
					items.append({"role": role, "text": text, "phase": payload.get("phase")})
			return items, logFile.tell()
	except OSError:
		return [], offset


def _loadTranscript():
	return _loadTranscriptPath(_latestSessionPath())


def _roleLabel(role):
	if role == "user":
		return "User message"
	if role == "assistant":
		return "Assistant message"
	if role == "system":
		return "System message"
	return "Message"


def _summarize(message, limit=1200):
	text = " ".join(message["text"].split())
	if len(text) > limit:
		text = text[:limit].rstrip() + "..."
	return "%s. %s" % (_roleLabel(message["role"]), text)


def _topLevelWindowHandle(obj=None):
	if obj is None:
		try:
			obj = api.getFocusObject()
		except Exception:
			obj = None
	while obj:
		windowHandle = getattr(obj, "windowHandle", None)
		if windowHandle:
			try:
				if winUser.getWindowStyle(windowHandle) & winUser.WS_CAPTION:
					return windowHandle
			except Exception:
				pass
		obj = getattr(obj, "parent", None)
	try:
		return api.getForegroundObject().windowHandle
	except Exception:
		return None


def _restoreCodexFocus(windowHandle):
	if not windowHandle:
		return
	try:
		if ctypes.windll.user32.IsIconic(int(windowHandle)):
			ctypes.windll.user32.ShowWindow(int(windowHandle), 9)  # SW_RESTORE
	except Exception:
		pass
	try:
		winUser.setForegroundWindow(windowHandle)
	except Exception:
		pass


def _codexExecutablePath(appPath=None):
	def isDesktopAppPath(path):
		if not isinstance(path, str) or os.path.basename(path).lower() not in CHATGPT_PROCESS_NAMES:
			return False
		norm = os.path.normcase(path)
		return "windowsapps" in norm and os.sep + "app" + os.sep in norm and "localcache" not in norm

	candidates = []
	if isDesktopAppPath(appPath):
		candidates.append(appPath)
	else:
		try:
			foregroundAppPath = api.getForegroundObject().appModule.appPath
		except Exception:
			foregroundAppPath = None
		if isDesktopAppPath(foregroundAppPath):
			candidates.append(foregroundAppPath)
	try:
		with open(CODEX_EXECUTABLE_CACHE_PATH, "r", encoding="utf-8", errors="replace") as cacheFile:
			cachedPath = cacheFile.read().strip()
	except OSError:
		cachedPath = ""
	if isDesktopAppPath(cachedPath):
		candidates.append(cachedPath)
	programFiles = os.environ.get("ProgramFiles")
	if programFiles:
		windowsAppsDir = os.path.join(programFiles, "WindowsApps")
		if os.path.isdir(windowsAppsDir):
			try:
				for entry in os.scandir(windowsAppsDir):
					if not entry.is_dir():
						continue
					if not entry.name.startswith("OpenAI.Codex_"):
						continue
					# New builds ship ChatGPT.exe; retain Codex.exe as a fallback
					# for older installed packages.
					candidates.append(os.path.join(entry.path, "app", "ChatGPT.exe"))
					candidates.append(os.path.join(entry.path, "app", "Codex.exe"))
			except OSError:
				pass
	for candidate in candidates:
		if isDesktopAppPath(candidate) and os.path.isfile(candidate):
			try:
				with open(CODEX_EXECUTABLE_CACHE_PATH, "w", encoding="utf-8") as cacheFile:
					cacheFile.write(candidate)
			except OSError:
				pass
			return candidate
	return None


def _launchCodex(args, appPath=None):
	exe = _codexExecutablePath(appPath)
	if not exe:
		return False
	try:
		subprocess.Popen([exe] + list(args), close_fds=True)
	except OSError:
		return False
	return True


def _launchCodexUrl(url):
	try:
		os.startfile(url)
	except OSError:
		return False
	return True


def _sendGesture(gestureName, itemName):
	try:
		for name in gestureName.split(","):
			keyboardHandler.KeyboardInputGesture.fromName(name).send()
	except Exception:
		ui.message("%s is unavailable" % itemName)


def _reloadCodexWorkspaceRoots(windowHandle):
	if not windowHandle:
		return False
	_restoreCodexFocus(windowHandle)
	wx.CallLater(120, _sendGesture, "control+r", "Reload")
	return True


def _restartCodex(windowHandle, appPath=None):
	exe = _codexExecutablePath(appPath)
	if not exe:
		return False
	try:
		if windowHandle:
			ctypes.windll.user32.PostMessageW(int(windowHandle), 0x0010, 0, 0)
	except Exception:
		pass
	wx.CallLater(1200, _launchCodex, [], appPath)
	return True


def _windowProcessId(windowHandle):
	if not windowHandle:
		return None
	processId = ctypes.c_ulong(0)
	try:
		ctypes.windll.user32.GetWindowThreadProcessId(int(windowHandle), ctypes.byref(processId))
	except Exception:
		return None
	return int(processId.value) or None


def _windowProcessPath(windowHandle):
	"""Return the executable path that owns a top-level window."""
	processId = _windowProcessId(windowHandle)
	if not processId:
		return None
	processHandle = None
	try:
		processHandle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(processId))
		if not processHandle:
			return None
		buffer = ctypes.create_unicode_buffer(32768)
		size = ctypes.c_ulong(len(buffer))
		if not ctypes.windll.kernel32.QueryFullProcessImageNameW(
			processHandle,
			0,
			buffer,
			ctypes.byref(size),
		):
			return None
		return buffer.value or None
	except Exception:
		return None
	finally:
		if processHandle:
			try:
				ctypes.windll.kernel32.CloseHandle(processHandle)
			except Exception:
				pass


def _waitForProcessExit(processId, timeoutSeconds):
	if not processId:
		return False
	processHandle = None
	try:
		processHandle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, int(processId))
		if not processHandle:
			return True
		result = ctypes.windll.kernel32.WaitForSingleObject(processHandle, max(0, int(timeoutSeconds * 1000)))
		return result == 0
	except Exception:
		return False
	finally:
		if processHandle:
			try:
				ctypes.windll.kernel32.CloseHandle(processHandle)
			except Exception:
				pass


def _isProcessRunning(processId):
	"""Return whether a remembered same-user desktop process is still alive."""
	return bool(processId) and not _waitForProcessExit(processId, 0)


def _waitForWindowClosed(windowHandle, timeoutSeconds):
	if not windowHandle:
		return False
	deadline = time.time() + max(0.0, float(timeoutSeconds))
	while time.time() <= deadline:
		try:
			if not ctypes.windll.user32.IsWindow(int(windowHandle)):
				return True
		except Exception:
			return True
		time.sleep(0.1)
	return False


def _restartCodexWithStateMutation(windowHandle, mutate, appPath=None, waitSeconds=5.0):
	if not callable(mutate):
		return False
	exe = _codexExecutablePath(appPath)
	if not exe:
		return False
	processId = _windowProcessId(windowHandle)
	if processId is None:
		return False
	try:
		ctypes.windll.user32.PostMessageW(int(windowHandle), 0x0010, 0, 0)
	except Exception:
		return False
	if not _waitForProcessExit(processId, waitSeconds):
		return False
	if not mutate():
		return False
	return _launchCodex([], appPath)


def _restartCodexWithStateMutationAsync(windowHandle, mutate, appPath=None, waitSeconds=15.0, onComplete=None):
	if not callable(mutate):
		return False
	exe = _codexExecutablePath(appPath)
	if not exe:
		return False
	if not windowHandle:
		return False
	try:
		ctypes.windll.user32.PostMessageW(int(windowHandle), 0x0010, 0, 0)
	except Exception:
		return False

	def worker():
		success = False
		if _waitForWindowClosed(windowHandle, waitSeconds):
			time.sleep(0.5)
			if mutate():
				success = _launchCodex([], appPath)
		if callable(onComplete):
			try:
				wx.CallAfter(onComplete, success)
			except Exception:
				try:
					onComplete(success)
				except Exception:
					pass

	thread = threading.Thread(target=worker, name="ChatGPTCodexStateMutationRestart", daemon=True)
	thread.start()
	return True


def _visibleUiAutomationControl(rootControl, controlTypeName, name=None, namePrefix=None, maxDepth=30):
	if uiautomation is None or rootControl is None:
		return None
	try:
		for control, _depth in uiautomation.WalkControl(rootControl, maxDepth=maxDepth):
			try:
				rect = control.BoundingRectangle
				controlName = control.Name
				if rect.right <= 0 or rect.bottom <= 0:
					continue
				if control.ControlTypeName != controlTypeName:
					continue
				if name is not None and controlName != name:
					continue
				if namePrefix is not None and not controlName.startswith(namePrefix):
					continue
				return control
			except Exception:
				continue
	except Exception:
		return None
	return None


def _codexWindowControl():
	if uiautomation is None:
		return None
	# The visible top-level window is now named ChatGPT.  Try the legacy Codex
	# title second so the add-on continues to work across staggered app updates.
	for windowName in ("ChatGPT", "Codex"):
		try:
			window = uiautomation.WindowControl(
				searchDepth=1,
				ClassName="Chrome_WidgetWin_1",
				Name=windowName,
			)
			if window.Exists(1):
				return window
		except Exception:
			continue
	return None


def _findCodexDesktopWindow():
	"""Find ChatGPT with Win32/UIA only; safe for a startup worker thread."""
	# ChatGPT's desktop window uses a stable Chromium class and exact title.
	# Validate the owning executable so a similarly named browser window is not
	# mistaken for the desktop application.
	for windowName in ("ChatGPT", "Codex"):
		try:
			handle = ctypes.windll.user32.FindWindowW("Chrome_WidgetWin_1", windowName)
		except Exception:
			handle = 0
		if not handle:
			continue
		path = _windowProcessPath(handle)
		if os.path.basename(path or "").lower() in CHATGPT_PROCESS_NAMES:
			return int(handle), path

	# Retain the existing UI Automation lookup as a fallback for app builds
	# whose Chromium window class changes.
	window = _codexWindowControl()
	if window is not None:
		for attribute in ("NativeWindowHandle", "Handle"):
			try:
				handle = int(getattr(window, attribute, 0) or 0)
			except Exception:
				handle = 0
			if not handle:
				continue
			path = _windowProcessPath(handle)
			if os.path.basename(path or "").lower() in CHATGPT_PROCESS_NAMES:
				return handle, path
	return None, None


def _codexWindowDetails():
	"""Find the open ChatGPT Codex window even when another app has focus."""
	if _isChatGPTForeground():
		try:
			root = api.getForegroundObject()
		except Exception:
			root = None
		handle = _topLevelWindowHandle(root)
		try:
			path = root.appModule.appPath
		except Exception:
			path = None
		if handle:
			return handle, path or _windowProcessPath(handle)
	return _findCodexDesktopWindow()


def _finishColdCodexOpen(url, windowHandle):
	"""Complete a delayed task/project deep link on NVDA's main thread."""
	_restoreCodexFocus(windowHandle)
	if not _launchCodexUrl(url):
		ui.message("ChatGPT opened, but the selected task or project could not be activated")
		return
	wx.CallLater(500, _restoreCodexFocus, windowHandle)


def _openCodexUrlForSelection(url, windowHandle=None, appPath=None, timeoutSeconds=15.0):
	"""Open a selection now, or cold-start ChatGPT and apply it when ready."""
	if windowHandle:
		_restoreCodexFocus(windowHandle)
		return _launchCodexUrl(url)
	exe = _codexExecutablePath(appPath)
	if not exe or not _launchCodex([], exe):
		return False

	def worker():
		deadline = time.time() + max(1.0, float(timeoutSeconds))
		while time.time() < deadline:
			handle, _path = _findCodexDesktopWindow()
			if handle:
				# The Chromium window appears shortly before its deep-link listener is
				# ready. Give the renderer one brief settling interval.
				time.sleep(1.0)
				wx.CallAfter(_finishColdCodexOpen, url, handle)
				return
			time.sleep(0.25)
		wx.CallAfter(ui.message, "ChatGPT started, but its window was not ready for the selected task or project")

	thread = threading.Thread(target=worker, name="ChatGPTCodexColdSelection", daemon=True)
	thread.start()
	return True


def _codexRootWebArea():
	window = _codexWindowControl()
	if window is None:
		return None, None
	try:
		rootWebArea = window.DocumentControl(AutomationId="RootWebArea")
		if rootWebArea.Exists(1):
			return window, rootWebArea
	except Exception:
		return window, None
	return window, None


def _nvdaObjectChildren(obj):
	"""Yield direct NVDA-object children without materializing a large tree."""
	try:
		child = obj.firstChild
	except Exception:
		child = None
	seen = set()
	while child is not None and id(child) not in seen:
		seen.add(id(child))
		yield child
		try:
			child = child.next
		except Exception:
			child = None


def _walkNvdaObjectTree(root, maxNodes=5000):
	"""Depth-first walk of the current Chromium accessibility tree."""
	if root is None:
		return
	stack = [root]
	visited = set()
	count = 0
	while stack and count < maxNodes:
		obj = stack.pop()
		identity = id(obj)
		if identity in visited:
			continue
		visited.add(identity)
		count += 1
		yield obj
		children = list(_nvdaObjectChildren(obj))
		stack.extend(reversed(children))


def _nvdaRoleText(obj):
	try:
		return str(obj.role).lower()
	except Exception:
		return ""


def _nvdaObjectName(obj):
	try:
		name = obj.name
	except Exception:
		name = ""
	return name.strip() if isinstance(name, str) else ""


def _hasNvdaListAncestor(obj):
	"""Return True for task-title controls that belong to the sidebar list."""
	try:
		parent = obj.parent
	except Exception:
		parent = None
	seen = set()
	while parent is not None and id(parent) not in seen:
		seen.add(id(parent))
		role = _nvdaRoleText(parent).replace(" ", "")
		if role in ("list", "listitem"):
			return True
		try:
			parent = parent.parent
		except Exception:
			parent = None
	return False


def _currentVisibleCodexTaskId(tasks):
	"""Identify the task shown in the foreground Codex content surface.

	The current task name is exposed as a toolbar button outside the sidebar's
	task list.  Matching that live control avoids guessing from transcript
	recency or from the last task the add-on itself opened.
	"""
	root = _chatgptForegroundRoot()
	if root is None:
		return None
	tasksByTitle = {}
	for task in tasks or []:
		title = _normalizeChatText(task.get("title") if isinstance(task, dict) else "").casefold()
		if title:
			tasksByTitle.setdefault(title, []).append(task)
	for obj in _walkNvdaObjectTree(root):
		role = _nvdaRoleText(obj)
		if "button" not in role and "heading" not in role:
			continue
		title = _normalizeChatText(_nvdaObjectName(obj)).casefold()
		matches = tasksByTitle.get(title, [])
		if len(matches) != 1 or _hasNvdaListAncestor(obj):
			continue
		taskId = matches[0].get("id")
		if isinstance(taskId, str) and taskId:
			return taskId
	return None


def _normalizeChatText(text):
	return " ".join((text or "").split())


def _isChatResponseActivity(text, role):
	"""Return whether an accessible row is ChatGPT's progress, not its reply."""
	normalized = _normalizeChatText(text)
	if not normalized or normalized in CHAT_RESPONSE_STATUS_NAMES:
		return False
	# A real ARIA status/progress control is authoritative. The desktop app
	# sometimes renders these progress cards as ordinary static text instead,
	# hence the conservative text fallback below.
	if "status" in role or "progress" in role:
		return True
	return len(normalized) <= 240 and bool(CHAT_RESPONSE_ACTIVITY_RE.match(normalized))


def _chatgptForegroundRoot():
	try:
		root = api.getForegroundObject()
	except Exception:
		return None
	try:
		appPath = root.appModule.appPath
	except Exception:
		appPath = None
	if os.path.basename(appPath or "").lower() not in CHATGPT_PROCESS_NAMES:
		return None
	return root


def _findChatComposer(root=None):
	"""Find only the consumer Chat composer, never Work or Codex composers."""
	root = root or _chatgptForegroundRoot()
	for obj in _walkNvdaObjectTree(root):
		role = _nvdaRoleText(obj)
		if "edit" in role and _nvdaObjectName(obj) == CHAT_COMPOSER_NAME:
			return obj
	return None


def _findChatSendButton(root=None):
	root = root or _chatgptForegroundRoot()
	for obj in _walkNvdaObjectTree(root):
		role = _nvdaRoleText(obj)
		if "button" in role and _nvdaObjectName(obj) in CHAT_SEND_BUTTON_NAMES:
			return obj
	return None


def _sendUnicodeText(text):
	"""Type Unicode through SendInput without replacing the user's clipboard."""
	if not text:
		return True

	ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

	class MOUSEINPUT(ctypes.Structure):
		_fields_ = (
			("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
			("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ULONG_PTR),
		)

	class KEYBDINPUT(ctypes.Structure):
		_fields_ = (
			("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
			("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ULONG_PTR),
		)

	class HARDWAREINPUT(ctypes.Structure):
		_fields_ = (("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort), ("wParamH", ctypes.c_ushort))

	class INPUTUNION(ctypes.Union):
		_fields_ = (("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT))

	class INPUT(ctypes.Structure):
		_fields_ = (("type", ctypes.c_ulong), ("data", INPUTUNION))

	INPUT_KEYBOARD = 1
	KEYEVENTF_KEYUP = 0x0002
	KEYEVENTF_UNICODE = 0x0004

	def sendChunk(chunk):
		if not chunk:
			return True
		units = chunk.encode("utf-16-le", errors="surrogatepass")
		inputs = []
		for offset in range(0, len(units), 2):
			scan = units[offset] | (units[offset + 1] << 8)
			for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
				item = INPUT()
				item.type = INPUT_KEYBOARD
				item.data.ki = KEYBDINPUT(0, scan, flags, 0, 0)
				inputs.append(item)
		array = (INPUT * len(inputs))(*inputs)
		return ctypes.windll.user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT)) == len(inputs)

	parts = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
	for index, part in enumerate(parts):
		for start in range(0, len(part), 256):
			if not sendChunk(part[start:start + 256]):
				return False
		if index < len(parts) - 1:
			try:
				keyboardHandler.KeyboardInputGesture.fromName("shift+enter").send()
			except Exception:
				return False
	return True


def _chatResponseRows(root):
	"""Return leaf-like text rows plus the consumer-composer boundary."""
	rows = []
	responding = False
	for obj in _walkNvdaObjectTree(root):
		role = _nvdaRoleText(obj)
		name = _nvdaObjectName(obj)
		if not name:
			continue
		if name in CHAT_RESPONSE_STATUS_NAMES or ("button" in role and name == "Stop"):
			responding = True
		if "edit" in role and name == CHAT_COMPOSER_NAME:
			rows.append(("composer", name, role))
			continue
		if any(kind in role for kind in ("statictext", "static text", "heading", "link", "status", "progress")):
			normalized = _normalizeChatText(name)
			if normalized and (not rows or rows[-1][0] != "text" or rows[-1][1] != normalized):
				rows.append(("text", normalized, role))
	return rows, responding


def _chatResponseState(root, prompt):
	"""Extract the answer and progress cards following the just-sent user turn."""
	rows, responding = _chatResponseRows(root)
	promptText = _normalizeChatText(prompt)
	composerIndex = next((index for index, row in enumerate(rows) if row[0] == "composer"), len(rows))
	textRows = [row for row in rows[:composerIndex] if row[0] == "text"]

	# Chromium normally exposes a user bubble as one static-text object. For
	# multiline prompts, accept a short run of adjacent rows as the same text.
	matchEnd = None
	for start in range(len(textRows)):
		combined = ""
		for end in range(start, min(len(textRows), start + 12)):
			combined = _normalizeChatText(" ".join((combined, textRows[end][1])))
			if combined == promptText:
				matchEnd = end + 1
				break
			if promptText and len(combined) > len(promptText) + 80:
				break
	if matchEnd is None:
		return "", responding, []

	activities = []
	answerRows = []
	for _kind, text, role in textRows[matchEnd:]:
		if text in CHAT_RESPONSE_STATUS_NAMES:
			continue
		if _isChatResponseActivity(text, role):
			if text not in activities:
				activities.append(text)
			continue
		if answerRows and answerRows[-1] == text:
			continue
		answerRows.append(text)
	return "\n".join(answerRows).strip(), responding, activities


def _chatResponseText(root, prompt):
	"""Extract only the rendered assistant answer following a user turn."""
	response, responding, _activities = _chatResponseState(root, prompt)
	return response, responding


def _codexProjectActionButton(rootWebArea, projectName, projectPath=None):
	if rootWebArea is None:
		return None
	candidateNames = []
	for value in (projectName, _projectDisplayName(projectPath) if projectPath else None, os.path.basename(projectPath) if projectPath else None):
		if not isinstance(value, str):
			continue
		text = value.strip()
		if text and text not in candidateNames:
			candidateNames.append(text)
	for candidate in candidateNames:
		try:
			button = rootWebArea.ButtonControl(Name="Project actions for %s" % candidate)
			if button.Exists(1):
				return button
		except Exception:
			continue
	return None


def _openCodexProjectActions(windowControl, actionButton):
	if windowControl is None or actionButton is None:
		return None
	for _attempt in range(3):
		try:
			pattern = actionButton.GetExpandCollapsePattern()
		except Exception:
			pattern = None
		try:
			if pattern is not None:
				try:
					pattern.Collapse()
					time.sleep(0.05)
				except Exception:
					pass
				pattern.Expand()
			else:
				actionButton.Click()
		except Exception:
			continue
		time.sleep(0.2)
		menu = _visibleUiAutomationControl(windowControl, "MenuControl", namePrefix="Project actions for ", maxDepth=20)
		if menu is not None:
			return menu
	return None


def _activateCodexProjectMenuItem(projectName, itemName, projectPath=None, windowHandle=None):
	_restoreCodexFocus(windowHandle)
	time.sleep(0.1)
	windowControl, rootWebArea = _codexRootWebArea()
	if windowControl is None or rootWebArea is None:
		return False
	actionButton = _codexProjectActionButton(rootWebArea, projectName, projectPath=projectPath)
	if actionButton is None:
		return False
	for _attempt in range(3):
		menu = _openCodexProjectActions(windowControl, actionButton)
		if menu is None:
			continue
		menuItem = _visibleUiAutomationControl(windowControl, "MenuItemControl", name=itemName, maxDepth=20)
		if menuItem is None:
			continue
		try:
			menuItem.Click()
			time.sleep(0.2)
			return True
		except Exception:
			continue
	return False


def _renameProjectInCodex(windowHandle, projectName, projectPath, label):
	text = label.strip()
	if not text:
		return False
	if not _activateCodexProjectMenuItem(projectName, "Rename project", projectPath=projectPath, windowHandle=windowHandle):
		return False
	windowControl, _rootWebArea = _codexRootWebArea()
	if windowControl is None:
		return False
	edit = _visibleUiAutomationControl(windowControl, "EditControl", name="Project name", maxDepth=35)
	saveButton = _visibleUiAutomationControl(windowControl, "ButtonControl", name="Save", maxDepth=35)
	if edit is None or saveButton is None:
		return False
	try:
		valuePattern = edit.GetValuePattern()
	except Exception:
		valuePattern = None
	if valuePattern is None:
		return False
	try:
		valuePattern.SetValue(text)
		saveButton.Click()
	except Exception:
		return False
	time.sleep(0.2)
	return True


def _removeProjectFromCodex(windowHandle, projectName, projectPath):
	if not _activateCodexProjectMenuItem(projectName, "Remove", projectPath=projectPath, windowHandle=windowHandle):
		return False
	windowControl, _rootWebArea = _codexRootWebArea()
	if windowControl is None:
		return False
	confirmButton = _visibleUiAutomationControl(windowControl, "ButtonControl", name="Remove", maxDepth=35)
	if confirmButton is None:
		return False
	try:
		confirmButton.Click()
	except Exception:
		return False
	time.sleep(0.2)
	return True


def _readJsonFile(path):
	try:
		with open(path, "r", encoding="utf-8", errors="replace") as jsonFile:
			return json.load(jsonFile)
	except (OSError, ValueError):
		return None


def _writeJsonFile(path, payload):
	folder = os.path.dirname(path)
	if folder:
		os.makedirs(folder, exist_ok=True)
	with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=folder, suffix=".tmp") as tempFile:
		json.dump(payload, tempFile, ensure_ascii=False, indent=2)
		tempFile.write("\n")
		tempPath = tempFile.name
	os.replace(tempPath, path)


def _readProjectPickerState():
	state = _readJsonFile(PROJECT_PICKER_STATE_PATH)
	return state if isinstance(state, dict) else {}


def _writeProjectPickerState(state):
	_writeJsonFile(PROJECT_PICKER_STATE_PATH, state)


def _canonicalPath(path):
	"""Return a comparable Windows path without an extended-length prefix."""
	path = os.path.abspath(path)
	if path.startswith("\\\\?\\UNC\\"):
		return "\\\\" + path[8:]
	if path.startswith("\\\\?\\"):
		return path[4:]
	return path


def _normalizePath(path):
	return os.path.normcase(_canonicalPath(path))


def _pathWithin(candidate, root):
	try:
		common = os.path.commonpath([_canonicalPath(candidate), _canonicalPath(root)])
	except ValueError:
		return False
	return _normalizePath(common) == _normalizePath(root)


def _responseText(response):
	if not isinstance(response, dict):
		return None
	error = response.get("error")
	if isinstance(error, dict):
		message = error.get("message")
		if isinstance(message, str) and message.strip():
			return message.strip()
	return None


def _appServerInitializePayload():
	return {
		"clientInfo": {
			"name": "agentDesktopAccessibility",
			"title": "Agent Desktop Accessibility for ChatGPT Codex",
			"version": "2.6.1",
		},
		"capabilities": {
			"experimentalApi": False,
			"optOutNotificationMethods": [
				"thread/started",
				"thread/status/changed",
				"account/updated",
				"account/rateLimits/updated",
			],
		},
	}


def _awaitResponse(messages, responseId, timeoutSeconds):
	deadline = time.monotonic() + timeoutSeconds
	while True:
		remaining = deadline - time.monotonic()
		if remaining <= 0:
			raise RuntimeError("Timed out waiting for the Codex app server")
		try:
			line = messages.get(timeout=remaining)
		except queue.Empty:
			raise RuntimeError("Timed out waiting for the Codex app server")
		if line is None:
			raise RuntimeError("Codex app server stopped before responding")
		try:
			payload = json.loads(line)
		except (TypeError, ValueError):
			continue
		# App-server may send account notifications between initialize and the
		# requested response. They have no matching id and are safe to ignore.
		if payload.get("id") != responseId:
			continue
		if payload.get("error"):
			raise RuntimeError(_responseText(payload) or "Codex rejected the app server request")
		result = payload.get("result")
		if not isinstance(result, dict):
			raise RuntimeError("Codex did not return an app server result")
		return result


def _runAppServerRequest(method, params=None, timeoutSeconds=APP_SERVER_TIMEOUT_SECONDS):
	command = _codexCliCommand() + ["app-server", "--listen", "stdio://"]
	proc = None
	try:
		proc = subprocess.Popen(
			command,
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
			text=True,
			bufsize=1,
			close_fds=True,
			creationflags=_subprocessCreationFlags(),
			startupinfo=_subprocessStartupInfo(),
		)
	except OSError as e:
		raise RuntimeError("Could not start Codex app server") from e

	messages = queue.Queue()

	def reader():
		try:
			for line in proc.stdout:
				if line.strip():
					messages.put(line)
		finally:
			messages.put(None)

	thread = threading.Thread(target=reader, name="CodexAppServerReader")
	thread.daemon = True
	thread.start()

	request = {"jsonrpc": "2.0", "id": "2", "method": method}
	if params is not None:
		request["params"] = params
	else:
		request["params"] = None
	payloads = (
		{"jsonrpc": "2.0", "id": "1", "method": "initialize", "params": _appServerInitializePayload()},
		{"jsonrpc": "2.0", "method": "initialized", "params": {}},
		request,
	)
	try:
		for payload in payloads:
			proc.stdin.write(json.dumps(payload) + "\n")
			proc.stdin.flush()
		_awaitResponse(messages, "1", timeoutSeconds)
		return _awaitResponse(messages, "2", timeoutSeconds)
	finally:
		try:
			if proc.stdin:
				proc.stdin.close()
		except Exception:
			pass
		try:
			proc.terminate()
		except Exception:
			pass
		try:
			proc.wait(timeout=1.0)
		except Exception:
			try:
				proc.kill()
			except Exception:
				pass


def _requestAppServerRateLimits():
	return _runAppServerRequest("account/rateLimits/read")


def _requestAppServerRateLimitReset(idempotencyKey, creditId=None):
	params = {"idempotencyKey": idempotencyKey}
	if creditId:
		params["creditId"] = creditId
	return _runAppServerRequest("account/rateLimitResetCredit/consume", params)


def _chooseRateLimitSnapshot(payload):
	if not isinstance(payload, dict):
		return None
	byLimitId = payload.get("rateLimitsByLimitId")
	if isinstance(byLimitId, dict):
		for key in ("codex", "default"):
			snapshot = byLimitId.get(key)
			if isinstance(snapshot, dict):
				return snapshot
		for snapshot in byLimitId.values():
			if isinstance(snapshot, dict):
				return snapshot
	snapshot = payload.get("rateLimits")
	if isinstance(snapshot, dict):
		return snapshot
	return None


def _readRateLimitSnapshot():
	snapshot = _chooseRateLimitSnapshot(_requestAppServerRateLimits())
	if not isinstance(snapshot, dict):
		raise RuntimeError("Usage limits unavailable")
	return snapshot


def _readResetCreditsSummary(payload):
	if not isinstance(payload, dict):
		return None
	summary = payload.get("rateLimitResetCredits")
	if not isinstance(summary, dict):
		return None
	try:
		availableCount = max(0, int(summary.get("availableCount", 0)))
	except (TypeError, ValueError):
		availableCount = 0
	details = summary.get("credits")
	credits = []
	if isinstance(details, list):
		credits = [credit for credit in details if isinstance(credit, dict)]
	return {
		"availableCount": availableCount,
		"credits": credits,
		"detailsAvailable": isinstance(details, list),
	}


def _readUsageState():
	payload = _requestAppServerRateLimits()
	snapshot = _chooseRateLimitSnapshot(payload)
	resetCredits = _readResetCreditsSummary(payload)
	if not isinstance(snapshot, dict):
		snapshot = {}
	windows = _findUsageWindows(snapshot)
	if windows["fiveHour"] is None and windows["weekly"] is None and resetCredits is None:
		raise RuntimeError("Usage limits unavailable")
	return {
		"snapshot": snapshot,
		"windows": windows,
		"reachedType": snapshot.get("rateLimitReachedType"),
		"resetCredits": resetCredits,
	}


def _findUsageWindows(snapshot):
	windows = {
		"fiveHour": None,
		"weekly": None,
	}
	if not isinstance(snapshot, dict):
		return windows
	for candidate in (snapshot.get("primary"), snapshot.get("secondary")):
		if not isinstance(candidate, dict):
			continue
		duration = candidate.get("windowDurationMins")
		if duration == FIVE_HOUR_WINDOW_MINS and windows["fiveHour"] is None:
			windows["fiveHour"] = candidate
		elif duration == WEEKLY_WINDOW_MINS and windows["weekly"] is None:
			windows["weekly"] = candidate
	return windows


def _formatResetTime(timestamp, durationMins=None):
	if not timestamp:
		return None
	try:
		moment = datetime.fromtimestamp(float(timestamp))
	except (OverflowError, TypeError, ValueError):
		return None
	if durationMins == WEEKLY_WINDOW_MINS:
		return moment.strftime("%A, %B %d, %Y %I:%M %p").replace(" 0", " ")
	return moment.strftime("%I:%M %p").lstrip("0")


def _formatWindowSummary(label, window):
	if not isinstance(window, dict):
		return "%s unavailable" % label
	usedPercent = window.get("usedPercent")
	if usedPercent is None:
		return "%s unavailable" % label
	remainingPercent = max(0, min(100, 100 - int(round(float(usedPercent)))))
	summary = "%s %s percent remaining" % (label, remainingPercent)
	resetText = _formatResetTime(window.get("resetsAt"), window.get("windowDurationMins"))
	if resetText:
		summary += ", resets at %s" % resetText
	return summary


def _formatResetCreditsSummary(resetCredits):
	if not isinstance(resetCredits, dict):
		return "Banked usage resets unavailable"
	count = resetCredits.get("availableCount", 0)
	return "Banked usage resets: %d" % count


def _formatUsageSummary(windows, reachedType, resetCredits=None):
	fiveHourText = _formatWindowSummary("5-hour usage limit", windows.get("fiveHour"))
	weeklyText = _formatWindowSummary("Weekly usage limit", windows.get("weekly"))
	parts = [fiveHourText, weeklyText, _formatResetCreditsSummary(resetCredits)]
	if reachedType == "rate_limit_reached":
		parts.append("5-hour limit reached")
	elif reachedType == "workspace_owner_credits_depleted":
		parts.append("Workspace owner credits depleted")
	elif reachedType == "workspace_member_credits_depleted":
		parts.append("Workspace member credits depleted")
	elif reachedType == "workspace_owner_usage_limit_reached":
		parts.append("Workspace owner usage limit reached")
	elif reachedType == "workspace_member_usage_limit_reached":
		parts.append("Workspace member usage limit reached")
	return ". ".join(parts) + "."


def _readUsageSummary():
	state = _readUsageState()
	return _formatUsageSummary(state["windows"], state["reachedType"], state["resetCredits"])


def _friendlyUsageError(error, fallback):
	message = str(error).strip()
	if "authentication required" in message.lower() or "not logged in" in message.lower():
		return "Codex CLI is not signed in. Sign in with the same ChatGPT account to read usage and banked resets"
	return message or fallback


def _availableResetCredits(resetCredits):
	if not isinstance(resetCredits, dict):
		return []
	credits = []
	for credit in resetCredits.get("credits") or []:
		if isinstance(credit, dict) and credit.get("status") == "available":
			credits.append(credit)
	credits.sort(key=lambda credit: (
		credit.get("expiresAt") is None,
		credit.get("expiresAt") if credit.get("expiresAt") is not None else float("inf"),
	))
	return credits


def _preferredResetCredit(resetCredits):
	credits = _availableResetCredits(resetCredits)
	return credits[0] if credits else None


def _formatResetCreditExpiry(credit):
	if not isinstance(credit, dict) or not credit.get("expiresAt"):
		return None
	try:
		moment = datetime.fromtimestamp(float(credit["expiresAt"]))
	except (OverflowError, TypeError, ValueError):
		return None
	return moment.strftime("%A, %B %d, %Y at %I:%M %p").replace(" 0", " ")


def _resetScopeText(windows):
	hasFiveHour = isinstance(windows, dict) and isinstance(windows.get("fiveHour"), dict)
	hasWeekly = isinstance(windows, dict) and isinstance(windows.get("weekly"), dict)
	if hasFiveHour and hasWeekly:
		return "This will request a reset of your current 5-hour and weekly usage limits."
	if hasFiveHour:
		return "This will request a reset of your current 5-hour usage limit."
	if hasWeekly:
		return "This will request a reset of your current weekly usage limit."
	return "This will request a reset of your current usage limits."


def _dedupePaths(paths):
	seen = set()
	result = []
	for path in paths or []:
		if not isinstance(path, str):
			continue
		norm = _normalizePath(path)
		if norm in seen:
			continue
		seen.add(norm)
		result.append(_canonicalPath(path))
	return result


def _projectDisplayName(path):
	base = os.path.basename(os.path.normpath(path)) or path
	parts = [part for part in base.split() if part]
	if len(parts) <= 3:
		return base
	return " ".join(parts[:3])


def _openFolderInExplorer(path):
	if not isinstance(path, str) or not path.strip():
		return False
	try:
		os.startfile(os.path.abspath(path))
	except OSError:
		return False
	return True


def _projectLabelsByPath(state, persisted):
	labels = {}
	for source in (state, persisted):
		if not isinstance(source, dict):
			continue
		for key in ("electron-workspace-root-labels", "workspace-root-labels"):
			mapping = source.get(key)
			if not isinstance(mapping, dict):
				continue
			for path, label in mapping.items():
				if not isinstance(path, str) or not isinstance(label, str):
					continue
				text = label.strip()
				if not text:
					continue
				labels[_normalizePath(path)] = {"path": os.path.abspath(path), "label": text}
	return labels


def _projectLabelForPath(root, labelsByPath):
	entry = labelsByPath.get(_normalizePath(root))
	if entry is None:
		return None
	return entry["label"]


def _withoutProjectLabel(mapping, root):
	if not isinstance(mapping, dict):
		return {}
	target = _normalizePath(root)
	result = {}
	for path, label in mapping.items():
		if not isinstance(path, str) or _normalizePath(path) == target:
			continue
		if isinstance(label, str) and label.strip():
			result[path] = label.strip()
	return result


def _writeProjectLabel(mapping, root, label):
	result = _withoutProjectLabel(mapping, root)
	text = label.strip()
	if text:
		result[os.path.abspath(root)] = text
	return result


def _hiddenProjectRoots(projectPickerState):
	return _dedupePaths(projectPickerState.get("hidden-project-roots") or [])


def _visibleProjectRoots(roots, hiddenRoots):
	return [root for root in _dedupePaths(roots) if not any(_pathWithin(root, hiddenRoot) for hiddenRoot in hiddenRoots)]


def _projectLabelsFromPickerState(projectPickerState):
	labels = {}
	mapping = projectPickerState.get("project-labels")
	if not isinstance(mapping, dict):
		return labels
	for path, label in mapping.items():
		if not isinstance(path, str) or not isinstance(label, str):
			continue
		text = label.strip()
		if not text:
			continue
		labels[_normalizePath(path)] = {"path": os.path.abspath(path), "label": text}
	return labels


def _storedProjectRoots(state, persisted):
	return _dedupePaths(
		(state.get("electron-saved-workspace-roots") or [])
		+ (persisted.get("electron-saved-workspace-roots") or [])
		+ (state.get("workspace-root-options") or [])
		+ (persisted.get("workspace-root-options") or [])
	)


def _sessionIndexRecords():
	records = []
	try:
		with open(SESSION_INDEX_PATH, "r", encoding="utf-8", errors="replace") as indexFile:
			for line in indexFile:
				line = line.strip()
				if not line:
					continue
				try:
					record = json.loads(line)
				except ValueError:
					continue
				if isinstance(record, dict) and isinstance(record.get("id"), str):
					records.append(record)
	except OSError:
		return []
	return records


def _stateDatabasePaths():
	"""Return Codex state databases newest-schema first.

	The numeric suffix is a schema generation and can change independently of
	the desktop app version.  Looking it up avoids baking ``state_5.sqlite``
	into the add-on forever.
	"""
	def schemaVersion(path):
		name = os.path.basename(path)
		try:
			return int(name[len("state_"):-len(".sqlite")])
		except (TypeError, ValueError):
			return -1

	return sorted(glob.glob(STATE_DB_PATTERN), key=schemaVersion, reverse=True)


def _systemPythonCommand():
	"""Find a system Python with SQLite for NVDA builds that omit it."""
	global _SYSTEM_PYTHON_COMMAND, _SYSTEM_PYTHON_PROBED
	if _SYSTEM_PYTHON_PROBED:
		return _SYSTEM_PYTHON_COMMAND
	_SYSTEM_PYTHON_PROBED = True
	localAppData = os.environ.get("LOCALAPPDATA", "")
	candidates = [["python"], ["python3"], ["py", "-3"]]
	for version in ("314", "313", "312", "311", "310"):
		candidates.append([
			os.path.join(localAppData, "Programs", "Python", "Python%s" % version, "python.exe")
		])
	for command in candidates:
		if not command[0]:
			continue
		try:
			process = subprocess.run(
				command + ["-c", "import sqlite3"],
				capture_output=True,
				timeout=5,
				creationflags=_subprocessCreationFlags(),
				startupinfo=_subprocessStartupInfo(),
			)
		except (OSError, subprocess.SubprocessError):
			continue
		if process.returncode == 0:
			_SYSTEM_PYTHON_COMMAND = command
			break
	return _SYSTEM_PYTHON_COMMAND


def _loadCodexTaskRowsWithSystemPython(databasePaths):
	"""Read task rows out of process when NVDA has no SQLite extension."""
	command = _systemPythonCommand()
	helper = os.path.join(os.path.dirname(__file__), "chatgptDb.py")
	if not command or not os.path.isfile(helper):
		return None
	for path in databasePaths:
		try:
			process = subprocess.run(
				command + [helper, path],
				capture_output=True,
				encoding="utf-8",
				errors="replace",
				timeout=15,
				creationflags=_subprocessCreationFlags(),
				startupinfo=_subprocessStartupInfo(),
			)
		except (OSError, subprocess.SubprocessError):
			continue
		if process.returncode != 0:
			continue
		try:
			payload = json.loads(process.stdout.strip() or "{}")
		except ValueError:
			continue
		rows = payload.get("tasks") if isinstance(payload, dict) else None
		if isinstance(rows, list):
			return [row for row in rows if isinstance(row, dict)]
	return None


def _taskProjectEntries():
	state = _readJsonFile(GLOBAL_STATE_PATH) or {}
	persisted = state.get("electron-persisted-atom-state")
	if not isinstance(persisted, dict):
		persisted = {}
	labelsByPath = _projectLabelsByPath(state, persisted)
	labelsByPath.update(_projectLabelsFromPickerState(_readProjectPickerState()))
	roots = _dedupePaths(
		(state.get("project-order") or [])
		+ (persisted.get("project-order") or [])
		+ (state.get("active-workspace-roots") or [])
		+ (persisted.get("active-workspace-roots") or [])
		+ _storedProjectRoots(state, persisted)
	)
	return [
		{
			"path": root,
			"name": _projectLabelForPath(root, labelsByPath) or _projectDisplayName(root),
		}
		for root in roots
	]


def _taskProjectName(cwd, projects):
	if not isinstance(cwd, str) or not cwd.strip():
		return "Standalone"
	best = None
	for project in projects or []:
		root = project.get("path")
		if not isinstance(root, str) or not _pathWithin(cwd, root):
			continue
		if best is None or len(root) > len(best[0]):
			best = (root, project.get("name"))
	if best is not None and isinstance(best[1], str) and best[1].strip():
		return best[1].strip()
	return _projectDisplayName(cwd)


def _cleanTaskTitle(*candidates):
	for candidate in candidates:
		if not isinstance(candidate, str):
			continue
		text = " ".join(candidate.split())
		if not text:
			continue
		if len(text) > 180:
			text = text[:177].rstrip() + "..."
		return text
	return "Untitled task"


def _loadCodexTasks():
	"""Load the top-level, non-archived tasks shown by ChatGPT Codex.

	Recent app builds moved the authoritative task index to ``state_*.sqlite``.
	Subagent rows share that table but are implementation details, so the
	accessible picker excludes them just like the app's main Tasks list.
	"""
	projects = _taskProjectEntries()
	databasePaths = _stateDatabasePaths()
	for path in databasePaths if sqlite3 is not None else []:
		connection = None
		try:
			connection = sqlite3.connect("file:" + path + "?mode=ro", uri=True, timeout=0.25)
			columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
			if not {"id", "archived"}.issubset(columns):
				continue

			def columnOrNull(name):
				return name if name in columns else "NULL"

			updatedColumn = next(
				(name for name in ("recency_at_ms", "updated_at_ms", "recency_at", "updated_at", "created_at_ms", "created_at") if name in columns),
				None,
			)
			where = ["archived = 0"]
			if "thread_source" in columns:
				where.append("(thread_source IS NULL OR thread_source != 'subagent')")
			query = "SELECT %s FROM threads WHERE %s" % (
				", ".join((
					"id",
					columnOrNull("rollout_path"),
					columnOrNull("name"),
					columnOrNull("title"),
					columnOrNull("preview"),
					columnOrNull("first_user_message"),
					columnOrNull("cwd"),
					updatedColumn or "0",
				)),
				" AND ".join(where),
			)
			if updatedColumn:
				query += " ORDER BY %s DESC" % updatedColumn
			tasks = []
			for taskId, rolloutPath, name, title, preview, firstMessage, cwd, updatedAt in connection.execute(query):
				if not isinstance(taskId, str) or not taskId.strip():
					continue
				tasks.append({
					"id": taskId,
					"rolloutPath": rolloutPath if isinstance(rolloutPath, str) else "",
					"title": _cleanTaskTitle(name, title, preview, firstMessage),
					"cwd": cwd if isinstance(cwd, str) else "",
					"project": _taskProjectName(cwd, projects),
					"updatedAt": updatedAt if isinstance(updatedAt, (int, float)) else 0,
				})
			return tasks
		except (OSError, sqlite3.Error):
			continue
		finally:
			if connection is not None:
				try:
					connection.close()
				except sqlite3.Error:
					pass

	if sqlite3 is None:
		rows = _loadCodexTaskRowsWithSystemPython(databasePaths)
		if rows is not None:
			tasks = []
			for row in rows:
				taskId = row.get("id")
				if not isinstance(taskId, str) or not taskId.strip():
					continue
				cwd = row.get("cwd") if isinstance(row.get("cwd"), str) else ""
				updatedAt = row.get("updatedAt")
				tasks.append({
					"id": taskId,
					"rolloutPath": row.get("rolloutPath") if isinstance(row.get("rolloutPath"), str) else "",
					"title": _cleanTaskTitle(row.get("name"), row.get("title"), row.get("preview"), row.get("firstUserMessage")),
					"cwd": cwd,
					"project": _taskProjectName(cwd, projects),
					"updatedAt": updatedAt if isinstance(updatedAt, (int, float)) else 0,
				})
			return tasks

	# Compatibility fallback for installations that have not created the
	# state database yet or have neither embedded SQLite nor a system Python.
	# The legacy index has no archive or subagent fields and may be incomplete.
	cwdsById = _sessionCwdById()
	result = []
	for record in sorted(_sessionIndexRecords(), key=lambda item: item.get("updated_at", ""), reverse=True):
		taskId = record.get("id")
		if not isinstance(taskId, str) or not taskId.strip():
			continue
		result.append({
			"id": taskId,
			"rolloutPath": _sessionPathForId(taskId) or "",
			"title": _cleanTaskTitle(record.get("thread_name")),
			"cwd": cwdsById.get(taskId, ""),
			"project": _taskProjectName(cwdsById.get(taskId), projects),
			"updatedAt": record.get("updated_at", ""),
		})
	return result


def _taskUpdatedEpoch(task):
	"""Return a task's last-use time in Unix seconds when available."""
	value = task.get("updatedAt") if isinstance(task, dict) else None
	if isinstance(value, (int, float)):
		return float(value) / 1000.0 if value > 100000000000 else float(value)
	if isinstance(value, str) and value.strip():
		text = value.strip()
		try:
			number = float(text)
			return number / 1000.0 if number > 100000000000 else number
		except ValueError:
			try:
				return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
			except (TypeError, ValueError, OverflowError):
				pass
	path = task.get("rolloutPath") if isinstance(task, dict) else None
	if isinstance(path, str) and path:
		try:
			return os.path.getmtime(path)
		except OSError:
			pass
	return None


def _taskCycleCandidates(tasks, now=None):
	"""Keep running/recent work in the arrow-key task cycle.

	A running task continuously updates its recency or transcript modification
	time, so it remains in this list. Completed, non-archived history ages out
	after seven days without changing the broader task/project pickers.
	"""
	cutoff = (time.time() if now is None else float(now)) - CODEX_TASK_CYCLE_MAX_AGE_SECONDS
	return [task for task in tasks or [] if (_taskUpdatedEpoch(task) or 0) >= cutoff]


def _taskListLabel(task):
	title = task.get("title") or "Untitled task"
	project = task.get("project") or "Standalone"
	return "%s — %s" % (title, project)


def _sessionCwdById():
	sessionCwds = {}
	if not os.path.isdir(SESSIONS_DIR):
		return sessionCwds
	for root, _dirs, files in os.walk(SESSIONS_DIR):
		for name in files:
			if not name.endswith(".jsonl"):
				continue
			path = os.path.join(root, name)
			try:
				with open(path, "r", encoding="utf-8", errors="replace") as sessionFile:
					firstLine = sessionFile.readline()
			except OSError:
				continue
			if not firstLine.strip():
				continue
			try:
				record = json.loads(firstLine)
			except ValueError:
				continue
			if record.get("type") != "session_meta":
				continue
			payload = record.get("payload")
			if not isinstance(payload, dict):
				continue
			sessionId = payload.get("id")
			cwd = payload.get("cwd")
			if isinstance(sessionId, str) and isinstance(cwd, str) and cwd.strip():
				sessionCwds[sessionId] = os.path.abspath(cwd)
	return sessionCwds


def _loadCodexProjects():
	state = _readJsonFile(GLOBAL_STATE_PATH) or {}
	persisted = state.get("electron-persisted-atom-state")
	if not isinstance(persisted, dict):
		persisted = {}
	projectPickerState = _readProjectPickerState()
	labelsByPath = _projectLabelsFromPickerState(projectPickerState)
	labelsByPath.update(_projectLabelsByPath(state, persisted))
	hiddenRoots = _hiddenProjectRoots(projectPickerState)
	projectOrder = _dedupePaths((state.get("project-order") or []) + (persisted.get("project-order") or []))
	activeRoots = _dedupePaths((state.get("active-workspace-roots") or []) + (persisted.get("active-workspace-roots") or []))
	savedRoots = _storedProjectRoots(state, persisted)
	explicitRoots = _dedupePaths(projectOrder + activeRoots + savedRoots)
	allRoots = list(explicitRoots)
	activeRoot = activeRoots[0] if activeRoots else (allRoots[0] if allRoots else None)

	projects = []
	threadsByRoot = {root: [] for root in allRoots}
	# Use the same authoritative source as the active-task dialog. Recent
	# ChatGPT builds do not write every task to session_index.jsonl, which
	# previously left projects (notably voice-created ones) with an empty
	# session list even though those tasks appeared in the app and task dialog.
	# _loadCodexTasks reads state_*.sqlite and retains the JSONL compatibility
	# fallback for older installations.
	tasks = _loadCodexTasks()
	sessionRoots = []
	for task in tasks:
		sessionId = task.get("id")
		if not isinstance(sessionId, str):
			continue
		cwd = task.get("cwd")
		if not cwd:
			continue
		if any(_pathWithin(cwd, hiddenRoot) for hiddenRoot in hiddenRoots):
			continue
		if any(_pathWithin(cwd, root) for root in explicitRoots + sessionRoots):
			continue
		sessionRoots.append(_canonicalPath(cwd))

	if sessionRoots:
		allRoots = _dedupePaths(allRoots + sessionRoots)
		threadsByRoot.update({root: [] for root in sessionRoots})
	for task in tasks:
		sessionId = task.get("id")
		if not isinstance(sessionId, str):
			continue
		cwd = task.get("cwd")
		if not cwd:
			continue
		root = None
		for candidate in allRoots:
			if _pathWithin(cwd, candidate):
				if root is None or len(candidate) > len(root):
					root = candidate
		if root is None:
			continue
		threadName = task.get("title")
		if not isinstance(threadName, str) or not threadName.strip():
			threadName = "Thread %s" % sessionId[:8]
		updatedAt = task.get("updatedAt")
		threadsByRoot.setdefault(root, []).append({
			"id": sessionId,
			"rolloutPath": task.get("rolloutPath", ""),
			"title": threadName.strip(),
			"updatedAt": updatedAt if isinstance(updatedAt, (int, float, str)) else 0,
			"cwd": cwd,
		})

	for root in allRoots:
		threads = threadsByRoot.get(root, [])
		threads.sort(key=lambda item: item.get("updatedAt", ""), reverse=True)
		label = _projectLabelForPath(root, labelsByPath) or _projectDisplayName(root)
		projects.append({
			"path": root,
			"name": label,
			"threads": threads,
		})

	return {
		"projects": projects,
		"activeRoot": activeRoot,
	}


def _setActiveProjectRoot(root):
	state = _readJsonFile(GLOBAL_STATE_PATH)
	if not isinstance(state, dict):
		return False
	projectPickerState = _readProjectPickerState()
	persisted = state.get("electron-persisted-atom-state")
	if not isinstance(persisted, dict):
		persisted = {}
		state["electron-persisted-atom-state"] = persisted
	projects = _dedupePaths((state.get("project-order") or []) + (persisted.get("project-order") or []))
	activeRoots = _dedupePaths(state.get("active-workspace-roots") or persisted.get("active-workspace-roots") or [])
	savedRoots = _storedProjectRoots(state, persisted)
	root = os.path.abspath(root)
	projects = [project for project in projects if _normalizePath(project) != _normalizePath(root)]
	projects.insert(0, root)
	activeRoots = [project for project in activeRoots if _normalizePath(project) != _normalizePath(root)]
	activeRoots.insert(0, root)
	savedRoots = [project for project in savedRoots if _normalizePath(project) != _normalizePath(root)]
	savedRoots.insert(0, root)
	state["project-order"] = projects
	state["active-workspace-roots"] = activeRoots
	state["electron-saved-workspace-roots"] = savedRoots
	state["workspace-root-options"] = savedRoots
	persisted["project-order"] = projects
	persisted["active-workspace-roots"] = activeRoots
	persisted["electron-saved-workspace-roots"] = list(savedRoots)
	persisted["workspace-root-options"] = list(savedRoots)
	projectPickerState["hidden-project-roots"] = [hiddenRoot for hiddenRoot in _hiddenProjectRoots(projectPickerState) if _normalizePath(hiddenRoot) != _normalizePath(root)]
	try:
		_writeJsonFile(GLOBAL_STATE_PATH, state)
		_writeProjectPickerState(projectPickerState)
	except OSError:
		return False
	return True


def _removeProjectRoot(root):
	state = _readJsonFile(GLOBAL_STATE_PATH)
	if not isinstance(state, dict):
		return False
	projectPickerState = _readProjectPickerState()
	persisted = state.get("electron-persisted-atom-state")
	if not isinstance(persisted, dict):
		persisted = {}
		state["electron-persisted-atom-state"] = persisted
	root = os.path.abspath(root)
	target = _normalizePath(root)

	def remove_path_list(values):
		return [value for value in _dedupePaths(values) if _normalizePath(value) != target]

	savedRoots = remove_path_list(_storedProjectRoots(state, persisted))
	state["project-order"] = remove_path_list((state.get("project-order") or []) + (persisted.get("project-order") or []))
	activeRoots = remove_path_list((state.get("active-workspace-roots") or []) + (persisted.get("active-workspace-roots") or []))
	activeRoots = [value for value in activeRoots if _normalizePath(value) in set(_normalizePath(project) for project in savedRoots)]
	if not activeRoots and savedRoots:
		activeRoots = [savedRoots[0]]
	state["active-workspace-roots"] = activeRoots
	state["workspace-root-options"] = list(savedRoots)
	state["electron-saved-workspace-roots"] = list(savedRoots)
	state["electron-workspace-root-labels"] = _withoutProjectLabel(state.get("electron-workspace-root-labels"), root)
	state["workspace-root-labels"] = _withoutProjectLabel(state.get("workspace-root-labels"), root)
	state["pinned-project-ids"] = remove_path_list(state.get("pinned-project-ids") or [])
	persisted["project-order"] = list(state["project-order"])
	persisted["active-workspace-roots"] = list(state["active-workspace-roots"])
	persisted["electron-saved-workspace-roots"] = list(savedRoots)
	persisted["workspace-root-options"] = list(savedRoots)
	persisted["electron-workspace-root-labels"] = _withoutProjectLabel(persisted.get("electron-workspace-root-labels"), root)
	persisted["workspace-root-labels"] = _withoutProjectLabel(persisted.get("workspace-root-labels"), root)
	projectPickerState["project-labels"] = _withoutProjectLabel(projectPickerState.get("project-labels"), root)
	projectPickerState["hidden-project-roots"] = _dedupePaths(_hiddenProjectRoots(projectPickerState) + [root])
	try:
		_writeJsonFile(GLOBAL_STATE_PATH, state)
		_writeProjectPickerState(projectPickerState)
	except OSError:
		return False
	return True


def _renameProjectRoot(root, label):
	state = _readJsonFile(GLOBAL_STATE_PATH)
	if not isinstance(state, dict):
		return False
	projectPickerState = _readProjectPickerState()
	persisted = state.get("electron-persisted-atom-state")
	if not isinstance(persisted, dict):
		persisted = {}
		state["electron-persisted-atom-state"] = persisted
	root = os.path.abspath(root)
	text = label.strip()
	if not text:
		return False
	state["electron-workspace-root-labels"] = _writeProjectLabel(state.get("electron-workspace-root-labels"), root, text)
	persisted["electron-workspace-root-labels"] = _writeProjectLabel(persisted.get("electron-workspace-root-labels"), root, text)
	state["workspace-root-labels"] = _writeProjectLabel(state.get("workspace-root-labels"), root, text)
	persisted["workspace-root-labels"] = _writeProjectLabel(persisted.get("workspace-root-labels"), root, text)
	projectPickerState["project-labels"] = _withoutProjectLabel(projectPickerState.get("project-labels"), root)
	projectPickerState["hidden-project-roots"] = [hiddenRoot for hiddenRoot in _hiddenProjectRoots(projectPickerState) if _normalizePath(hiddenRoot) != _normalizePath(root)]
	try:
		_writeJsonFile(GLOBAL_STATE_PATH, state)
		_writeProjectPickerState(projectPickerState)
	except OSError:
		return False
	return True


class _TaskDialog(wx.Dialog):
	def __init__(self, parent, windowHandle=None, codexPath=None, onTaskOpened=None):
		super(_TaskDialog, self).__init__(parent, title="Active ChatGPT Codex tasks", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
		self._windowHandle = windowHandle
		self._codexPath = codexPath
		self._onTaskOpened = onTaskOpened
		self._tasks = []
		self._initialFocusSet = False
		self._buildUi()
		self._refreshTasks()

	def _buildUi(self):
		rootSizer = wx.BoxSizer(wx.VERTICAL)
		label = wx.StaticText(self, label="Active tasks")
		self.taskList = wx.ListBox(self, style=wx.LB_SINGLE | wx.LB_NEEDED_SB)
		self.taskList.SetName("Active ChatGPT Codex tasks")
		self.taskList.Bind(wx.EVT_LISTBOX, self._onTaskSelected)
		self.taskList.Bind(wx.EVT_LISTBOX_DCLICK, self._onOpenTask)
		self.taskList.Bind(wx.EVT_KEY_DOWN, self._onTaskKeyDown)
		self.status = wx.StaticText(self, label="")
		self.status.SetName("Task picker status")

		buttonSizer = wx.BoxSizer(wx.HORIZONTAL)
		self.openButton = wx.Button(self, label="&Open Task")
		self.refreshButton = wx.Button(self, label="&Refresh")
		self.closeButton = wx.Button(self, id=wx.ID_CANCEL, label="&Close")
		self.openButton.Bind(wx.EVT_BUTTON, self._onOpenTask)
		self.refreshButton.Bind(wx.EVT_BUTTON, self._onRefresh)
		self.closeButton.Bind(wx.EVT_BUTTON, self._onClose)
		buttonSizer.Add(self.openButton, 0, wx.RIGHT, 8)
		buttonSizer.Add(self.refreshButton, 0, wx.RIGHT, 8)
		buttonSizer.AddStretchSpacer(1)
		buttonSizer.Add(self.closeButton, 0)

		rootSizer.Add(label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
		rootSizer.Add(self.taskList, 1, wx.EXPAND | wx.ALL, 10)
		rootSizer.Add(self.status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
		rootSizer.Add(buttonSizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
		self.SetSizerAndFit(rootSizer)
		self.SetMinSize((680, 400))
		self.Bind(wx.EVT_CHAR_HOOK, self._onCharHook)
		self.Bind(wx.EVT_ACTIVATE, self._onActivate)
		wx.CallLater(0, self._setInitialFocus)

	def _setInitialFocus(self):
		if self._initialFocusSet:
			return
		try:
			if self.taskList.GetCount() > 0 and self._tasks:
				self.taskList.SetSelection(0 if self.taskList.GetSelection() < 0 else self.taskList.GetSelection())
			self.taskList.SetFocus()
			self._initialFocusSet = True
		except Exception:
			pass

	def _onActivate(self, evt):
		if evt.GetActive():
			try:
				self.Raise()
			except Exception:
				pass
			wx.CallLater(0, self._setInitialFocus)
		evt.Skip()

	def _refreshTasks(self, preferredId=None):
		self._tasks = _loadCodexTasks()
		if not self._tasks:
			self.taskList.SetItems(["No active tasks found"])
			self.taskList.SetSelection(0)
			self.status.SetLabel("No active ChatGPT Codex tasks found")
			self.openButton.Enable(False)
			return
		self.taskList.SetItems([_taskListLabel(task) for task in self._tasks])
		selection = 0
		if preferredId:
			for index, task in enumerate(self._tasks):
				if task.get("id") == preferredId:
					selection = index
					break
		self.taskList.SetSelection(selection)
		self.status.SetLabel("%d active task%s, newest first" % (len(self._tasks), "" if len(self._tasks) == 1 else "s"))
		self.openButton.Enable(True)

	def _selectedTask(self):
		index = self.taskList.GetSelection()
		if index < 0 or index >= len(self._tasks):
			return None
		return self._tasks[index]

	def _onTaskSelected(self, evt):
		# The native wx.ListBox accessibility event already announces the item.
		# Calling ui.message here causes every selection to be spoken twice.
		evt.Skip()

	def _onTaskKeyDown(self, evt):
		if evt.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
			self._onOpenTask(None)
			return
		evt.Skip()

	def _onCharHook(self, evt):
		if evt.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
			try:
				focus = wx.Window.FindFocus()
			except Exception:
				focus = None
			if focus is self.taskList:
				self._onOpenTask(None)
				return
		evt.Skip()

	def _onOpenTask(self, evt):
		task = self._selectedTask()
		if task is None:
			ui.message("Choose a task first")
			return
		windowHandle = getattr(self, "_windowHandle", None)
		if not _openCodexUrlForSelection(
			"codex://threads/%s" % quote(task["id"], safe=""),
			windowHandle,
			getattr(self, "_codexPath", None),
		):
			ui.message("Could not open the selected ChatGPT Codex task")
			return
		callback = getattr(self, "_onTaskOpened", None)
		if callable(callback):
			callback(task)
		ui.message("%s %s" % ("Opening" if windowHandle else "Starting ChatGPT. Opening", task["title"]))
		self.EndModal(wx.ID_OK)
		wx.CallLater(0, _restoreCodexFocus, windowHandle)

	def _onRefresh(self, evt):
		selected = self._selectedTask()
		self._refreshTasks(preferredId=selected.get("id") if selected else None)
		self._setInitialFocus()

	def _onClose(self, evt):
		self.EndModal(wx.ID_CANCEL)


class _ProjectThreadDialog(wx.Dialog):
	def __init__(self, parent, windowHandle, codexPath=None, onProjectOpened=None, onTaskOpened=None):
		super(_ProjectThreadDialog, self).__init__(parent, title="ChatGPT Codex projects", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
		self._windowHandle = windowHandle
		self._codexPath = codexPath
		self._onProjectOpened = onProjectOpened
		self._onTaskOpened = onTaskOpened
		self._projects = []
		self._selectedProjectPath = None
		self._initialFocusSet = False
		self._buildUi()
		self._refreshProjects()

	def _buildUi(self):
		rootSizer = wx.BoxSizer(wx.VERTICAL)
		contentSizer = wx.BoxSizer(wx.HORIZONTAL)

		leftSizer = wx.BoxSizer(wx.VERTICAL)
		rightSizer = wx.BoxSizer(wx.VERTICAL)

		leftLabel = wx.StaticText(self, label="Projects")
		self.projectList = wx.ListBox(self, style=wx.LB_SINGLE | wx.LB_NEEDED_SB)
		self.projectList.SetName("ChatGPT Codex projects")
		self.projectList.Bind(wx.EVT_LISTBOX, self._onProjectSelected)
		self.projectList.Bind(wx.EVT_LISTBOX_DCLICK, self._onOpenProject)
		self.projectList.Bind(wx.EVT_KEY_DOWN, self._onProjectKeyDown)

		rightLabel = wx.StaticText(self, label="Tasks")
		self.threadList = wx.ListBox(self, style=wx.LB_SINGLE | wx.LB_NEEDED_SB)
		self.threadList.SetName("ChatGPT Codex tasks")
		self.threadList.Bind(wx.EVT_LISTBOX, self._onThreadSelected)
		self.threadList.Bind(wx.EVT_LISTBOX_DCLICK, self._onOpenThread)
		self.threadList.Bind(wx.EVT_KEY_DOWN, self._onThreadKeyDown)

		leftSizer.Add(leftLabel, 0, wx.BOTTOM, 4)
		leftSizer.Add(self.projectList, 1, wx.EXPAND)
		rightSizer.Add(rightLabel, 0, wx.BOTTOM, 4)
		rightSizer.Add(self.threadList, 1, wx.EXPAND)

		contentSizer.Add(leftSizer, 1, wx.EXPAND | wx.RIGHT, 10)
		contentSizer.Add(rightSizer, 1, wx.EXPAND)

		self.status = wx.StaticText(self, label="")
		self.status.SetName("Project picker status")

		buttonSizer = wx.BoxSizer(wx.HORIZONTAL)
		self.openProjectButton = wx.Button(self, label="Open &Project")
		self.openThreadButton = wx.Button(self, label="Open &Task")
		self.addProjectButton = wx.Button(self, label="&Add Project")
		self.renameProjectButton = wx.Button(self, label="Rena&me Project")
		self.removeProjectButton = wx.Button(self, label="Re&move Project")
		self.openProjectFolderButton = wx.Button(self, label="Open in &Explorer")
		self.newChatButton = wx.Button(self, label="New &Task")
		self.closeButton = wx.Button(self, id=wx.ID_CANCEL, label="&Close")
		self.openProjectButton.Bind(wx.EVT_BUTTON, self._onOpenProject)
		self.openThreadButton.Bind(wx.EVT_BUTTON, self._onOpenThread)
		self.addProjectButton.Bind(wx.EVT_BUTTON, self._onAddProject)
		self.renameProjectButton.Bind(wx.EVT_BUTTON, self._onRenameProject)
		self.removeProjectButton.Bind(wx.EVT_BUTTON, self._onRemoveProject)
		self.openProjectFolderButton.Bind(wx.EVT_BUTTON, self._onOpenProjectFolder)
		self.newChatButton.Bind(wx.EVT_BUTTON, self._onNewChat)
		self.closeButton.Bind(wx.EVT_BUTTON, self._onClose)

		buttonSizer.Add(self.openProjectButton, 0, wx.RIGHT, 8)
		buttonSizer.Add(self.openThreadButton, 0, wx.RIGHT, 8)
		buttonSizer.Add(self.addProjectButton, 0, wx.RIGHT, 8)
		buttonSizer.Add(self.renameProjectButton, 0, wx.RIGHT, 8)
		buttonSizer.Add(self.removeProjectButton, 0, wx.RIGHT, 8)
		buttonSizer.Add(self.openProjectFolderButton, 0, wx.RIGHT, 8)
		buttonSizer.Add(self.newChatButton, 0, wx.RIGHT, 8)
		buttonSizer.AddStretchSpacer(1)
		buttonSizer.Add(self.closeButton, 0)

		rootSizer.Add(contentSizer, 1, wx.EXPAND | wx.ALL, 10)
		rootSizer.Add(self.status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
		rootSizer.Add(buttonSizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
		self.SetSizerAndFit(rootSizer)
		self.SetMinSize((760, 420))
		self.Bind(wx.EVT_CHAR_HOOK, self._onCharHook)
		self.Bind(wx.EVT_ACTIVATE, self._onActivate)
		wx.CallLater(0, self._setInitialFocus)

	def _setInitialFocus(self):
		if self._initialFocusSet:
			return
		try:
			if self.projectList.GetCount() > 0:
				self.projectList.SetSelection(0 if self.projectList.GetSelection() < 0 else self.projectList.GetSelection())
			self.projectList.SetFocus()
			self._initialFocusSet = True
		except Exception:
			pass

	def _onActivate(self, evt):
		if evt.GetActive():
			try:
				self.Raise()
			except Exception:
				pass
			wx.CallLater(0, self._setInitialFocus)
		evt.Skip()

	def _refreshProjects(self, preferredPath=None):
		data = _loadCodexProjects()
		self._projects = data["projects"]
		self._selectedProjectPath = preferredPath or data["activeRoot"]
		projectLabels = []
		for project in self._projects:
			threadCount = len(project["threads"])
			label = "%s (%d task%s)" % (
				project["name"],
				threadCount,
				"" if threadCount == 1 else "s",
			)
			projectLabels.append(label)
		self.projectList.SetItems(projectLabels)

		if not self._projects:
			self.threadList.SetItems([])
			self.status.SetLabel("No ChatGPT Codex projects found")
			self.openProjectButton.Enable(False)
			self.openThreadButton.Enable(False)
			self.renameProjectButton.Enable(False)
			self.removeProjectButton.Enable(False)
			self.openProjectFolderButton.Enable(False)
			self.newChatButton.Enable(False)
			return

		index = 0
		if self._selectedProjectPath:
			for i, project in enumerate(self._projects):
				if _normalizePath(project["path"]) == _normalizePath(self._selectedProjectPath):
					index = i
					break
		self.projectList.SetSelection(index)
		self._selectedProjectPath = self._projects[index]["path"]
		self._refreshThreads()
		self.openProjectButton.Enable(True)
		self.renameProjectButton.Enable(True)
		self.removeProjectButton.Enable(True)
		self.openProjectFolderButton.Enable(True)
		self.newChatButton.Enable(True)

	def _selectedProject(self):
		if self.projectList.GetSelection() < 0 or self.projectList.GetSelection() >= len(self._projects):
			return None
		return self._projects[self.projectList.GetSelection()]

	def _selectedThread(self):
		project = self._selectedProject()
		if project is None:
			return None
		threads = project["threads"]
		index = self.threadList.GetSelection()
		if index < 0 or index >= len(threads):
			return None
		return threads[index]

	def _refreshThreads(self):
		project = self._selectedProject()
		if project is None:
			self.threadList.SetItems([])
			self.status.SetLabel("No project selected")
			self.openThreadButton.Enable(False)
			return
		threads = project["threads"]
		if threads:
			self.threadList.SetItems([thread["title"] for thread in threads])
			self.threadList.SetSelection(0)
			self.status.SetLabel("%s has %d task%s" % (project["name"], len(threads), "" if len(threads) == 1 else "s"))
			self.openThreadButton.Enable(True)
		else:
			self.threadList.SetItems(["No tasks found"])
			self.threadList.SetSelection(0)
			self.status.SetLabel("%s has no tasks yet" % project["name"])
			self.openThreadButton.Enable(False)

	def _onProjectSelected(self, evt):
		project = self._selectedProject()
		if project is not None:
			self._selectedProjectPath = project["path"]
		self._refreshThreads()
		evt.Skip()

	def _onThreadSelected(self, evt):
		# NVDA receives the native selection event from the list box directly.
		evt.Skip()

	def _onProjectKeyDown(self, evt):
		keyCode = evt.GetKeyCode()
		if keyCode in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
			self._onOpenProject(None)
			return
		evt.Skip()

	def _onThreadKeyDown(self, evt):
		keyCode = evt.GetKeyCode()
		if keyCode in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
			self._onOpenThread(None)
			return
		evt.Skip()

	def _onCharHook(self, evt):
		keyCode = evt.GetKeyCode()
		if keyCode not in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
			evt.Skip()
			return
		try:
			focus = wx.Window.FindFocus()
		except Exception:
			focus = None
		if focus is self.threadList:
			self._onOpenThread(None)
			return
		if focus is self.projectList:
			self._onOpenProject(None)
			return
		evt.Skip()

	def _onOpenProject(self, evt):
		project = self._selectedProject()
		if project is None:
			ui.message("Choose a project first")
			return
		if not _setActiveProjectRoot(project["path"]):
			ui.message("Could not activate the selected project")
			return
		windowHandle = getattr(self, "_windowHandle", None)
		if not _openCodexUrlForSelection(
			"codex://new?path=%s" % quote(project["path"], safe=""),
			windowHandle,
			getattr(self, "_codexPath", None),
		):
			ui.message("Could not open the selected project")
			return
		callback = getattr(self, "_onProjectOpened", None)
		if callable(callback):
			callback(project)
		ui.message("%s %s" % ("Opening" if windowHandle else "Starting ChatGPT. Opening", project["name"]))
		self.EndModal(wx.ID_OK)
		wx.CallLater(0, _restoreCodexFocus, windowHandle)

	def _onOpenThread(self, evt):
		project = self._selectedProject()
		thread = self._selectedThread()
		if project is None:
			ui.message("Choose a project first")
			return
		if thread is None:
			ui.message("Choose a task first")
			return
		if not _setActiveProjectRoot(project["path"]):
			ui.message("Could not activate the selected project")
			return
		windowHandle = getattr(self, "_windowHandle", None)
		if not _openCodexUrlForSelection(
			"codex://threads/%s" % quote(thread["id"], safe=""),
			windowHandle,
			getattr(self, "_codexPath", None),
		):
			ui.message("Could not open the selected task")
			return
		callback = getattr(self, "_onTaskOpened", None)
		if callable(callback):
			callback(thread)
		ui.message("%s %s" % ("Opening" if windowHandle else "Starting ChatGPT. Opening", thread["title"]))
		self.EndModal(wx.ID_OK)
		wx.CallLater(0, _restoreCodexFocus, windowHandle)

	def _onAddProject(self, evt):
		dlg = wx.DirDialog(self, message="Choose a ChatGPT Codex project folder")
		try:
			if dlg.ShowModal() != wx.ID_OK:
				return
			root = dlg.GetPath()
		finally:
			dlg.Destroy()
		if not root:
			return
		if not _setActiveProjectRoot(root):
			ui.message("Could not save the selected project")
			return
		self._refreshProjects(preferredPath=root)
		ui.message("%s added" % _projectDisplayName(root))

	def _onOpenProjectFolder(self, evt):
		project = self._selectedProject()
		if project is None:
			ui.message("Choose a project first")
			return
		if not _openFolderInExplorer(project["path"]):
			ui.message("Could not open the selected project folder")
			return
		ui.message("Opening folder for %s" % project["name"])

	def _onRenameProject(self, evt):
		project = self._selectedProject()
		if project is None:
			ui.message("Choose a project first")
			return
		dlg = wx.TextEntryDialog(self, "Enter a new project name", "Rename project", value=project["name"])
		try:
			if dlg.ShowModal() != wx.ID_OK:
				return
			label = dlg.GetValue()
		finally:
			dlg.Destroy()
		if not isinstance(label, str) or not label.strip():
			ui.message("Project name cannot be empty")
			return
		try:
			self.Hide()
		except Exception:
			pass
		renamed = _renameProjectInCodex(self._windowHandle, project["name"], project["path"], label)
		if not renamed:
			def onComplete(success):
				if not success:
					try:
						self.Show()
						self.Raise()
					except Exception:
						pass
					ui.message("Could not rename the selected project in ChatGPT")
					return
				self._refreshProjects(preferredPath=project["path"])
				ui.message("%s renamed to %s" % (project["name"], label.strip()))
				self.EndModal(wx.ID_OK)

			if not _restartCodexWithStateMutationAsync(
				self._windowHandle,
				lambda: _renameProjectRoot(project["path"], label),
				self._codexPath,
				onComplete=onComplete,
			):
				try:
					self.Show()
					self.Raise()
				except Exception:
					pass
				ui.message("Could not rename the selected project in ChatGPT")
			return
		self._refreshProjects(preferredPath=project["path"])
		ui.message("%s renamed to %s" % (project["name"], label.strip()))
		self.EndModal(wx.ID_OK)
		return

	def _onRemoveProject(self, evt):
		project = self._selectedProject()
		if project is None:
			ui.message("Choose a project first")
			return
		try:
			self.Hide()
		except Exception:
			pass
		removed = _removeProjectFromCodex(self._windowHandle, project["name"], project["path"])
		if not removed:
			def onComplete(success):
				if not success:
					try:
						self.Show()
						self.Raise()
					except Exception:
						pass
					ui.message("Could not remove the selected project from ChatGPT")
					return
				self._refreshProjects()
				ui.message("%s removed" % project["name"])
				self.EndModal(wx.ID_OK)

			if not _restartCodexWithStateMutationAsync(
				self._windowHandle,
				lambda: _removeProjectRoot(project["path"]),
				self._codexPath,
				onComplete=onComplete,
			):
				try:
					self.Show()
					self.Raise()
				except Exception:
					pass
				ui.message("Could not remove the selected project from ChatGPT")
			return
		self._refreshProjects()
		ui.message("%s removed" % project["name"])
		self.EndModal(wx.ID_OK)
		return

	def _onNewChat(self, evt):
		project = self._selectedProject()
		if project is None:
			ui.message("Choose a project first")
			return
		if not _setActiveProjectRoot(project["path"]):
			ui.message("Could not activate the selected project")
			return
		windowHandle = getattr(self, "_windowHandle", None)
		if not _openCodexUrlForSelection(
			"codex://new?path=%s" % quote(project["path"], safe=""),
			windowHandle,
			getattr(self, "_codexPath", None),
		):
			ui.message("Could not start a new task in the selected project")
			return
		callback = getattr(self, "_onProjectOpened", None)
		if callable(callback):
			callback(project)
		ui.message("%s a new task in %s" % (
			"Starting" if windowHandle else "Starting ChatGPT. Starting",
			project["name"],
		))
		self.EndModal(wx.ID_OK)
		wx.CallLater(0, _restoreCodexFocus, windowHandle)

	def _onClose(self, evt):
		self.EndModal(wx.ID_CANCEL)


class ChatGPTBackend(object):
	"""Codex helpers adapted for the renamed ChatGPT desktop application."""

	def __init__(self):
		self._transcriptPath = None
		self._messages = []
		self._index = None
		self._threadCycleIndex = None
		self._activeTaskId = None
		self._activeTranscriptPath = None
		self._pendingProjectPath = None
		self._pendingProjectSince = 0.0
		self._autoReadEnabled = True
		self._completionOffsets = None
		self._autoReadTimer = None
		self._codexWasFocused = False
		self._codexWindowHandle = None
		self._codexProcessId = None
		self._activeTaskMonitorTasks = []
		self._activeTaskMonitorRefreshedAt = 0.0
		self._autoReadTaskOffsets = None
		self._autoReadLastReasoningByPath = {}
		self._knownAutoReadTaskPaths = set()
		self._autoReadTaskCacheBaselined = False
		self._usageRequestSerial = 0
		self._chatRequestSerial = 0
		self._chatResponseLast = ""
		self._chatResponseStablePolls = 0
		self._chatActivityLast = ()
		self._chatRequestStartedAt = 0.0
		self._autoReadFirstPollLogged = False
		self._terminated = False
		self._scheduleAutoRead()

	def _clearTranscriptBuffer(self):
		self._transcriptPath = None
		self._messages = []
		self._index = None

	def _activateTask(self, task):
		"""Retarget message navigation and auto-read to an opened Codex task."""
		self._activeTaskId = task.get("id") if isinstance(task, dict) else None
		path = task.get("rolloutPath") if isinstance(task, dict) else None
		if not isinstance(path, str) or not path.strip() or not os.path.isfile(path):
			path = _sessionPathForId(self._activeTaskId)
		self._activeTranscriptPath = path
		self._pendingProjectPath = None
		self._pendingProjectSince = 0.0
		self._clearTranscriptBuffer()

	def _activateProject(self, project):
		"""Clear the old task buffer while Codex creates a task in a project."""
		path = project.get("path") if isinstance(project, dict) else None
		self._activeTaskId = None
		self._activeTranscriptPath = None
		self._pendingProjectPath = os.path.abspath(path) if isinstance(path, str) and path.strip() else None
		# Allow for the deep link creating the transcript just before the dialog
		# callback runs, while still rejecting old project history.
		self._pendingProjectSince = time.time() - 5.0
		self._clearTranscriptBuffer()

	def _resolveTranscriptPath(self):
		if self._activeTranscriptPath and os.path.isfile(self._activeTranscriptPath):
			return self._activeTranscriptPath
		if self._activeTaskId:
			self._activeTranscriptPath = _sessionPathForId(self._activeTaskId)
			return self._activeTranscriptPath
		if self._pendingProjectPath:
			candidate = _latestSessionPath()
			if not candidate:
				return None
			try:
				if os.path.getmtime(candidate) < self._pendingProjectSince:
					return None
			except OSError:
				return None
			metadata = _sessionMetadata(candidate)
			cwd = metadata.get("cwd")
			if not isinstance(cwd, str) or not _pathWithin(cwd, self._pendingProjectPath):
				return None
			self._activeTaskId = metadata.get("id")
			self._activeTranscriptPath = candidate
			self._pendingProjectPath = None
			self._pendingProjectSince = 0.0
			return candidate
		return _latestSessionPath()

	def _loadActiveTranscript(self):
		path = self._resolveTranscriptPath()
		if path is None and (self._activeTaskId or self._pendingProjectPath):
			return None, []
		return _loadTranscriptPath(path)

	def _scheduleAutoRead(self):
		"""Start one owned periodic timer for auto-read and completion checks."""
		if self._terminated or self._autoReadTimer is not None:
			return
		try:
			timer = _AutoReadTimer(self._autoReadCheck)
			if timer.Start(CODEX_AUTO_READ_POLL_MS) is False:
				raise RuntimeError("wx.Timer.Start returned false")
			self._autoReadTimer = timer
			log.info("Codex auto-read timer started: interval=%d ms", CODEX_AUTO_READ_POLL_MS)
		except Exception:
			self._autoReadTimer = None
			log.exception("Could not start the Codex auto-read timer")

	def _codexReadSessionActive(self):
		"""Remember Codex after it receives focus, until its window closes."""
		if _isChatGPTForeground():
			self._codexWasFocused = True
			self._codexWindowHandle = _topLevelWindowHandle()
			self._codexProcessId = _windowProcessId(self._codexWindowHandle)
			if not self._codexProcessId:
				try:
					foreground = api.getForegroundObject()
					self._codexProcessId = int(
						getattr(getattr(foreground, "appModule", None), "processID", 0) or 0
					) or None
				except Exception:
					self._codexProcessId = None
			return True
		if not self._codexWasFocused:
			return False
		processRunning = _isProcessRunning(self._codexProcessId)
		try:
			windowOpen = bool(
				self._codexWindowHandle
				and ctypes.windll.user32.IsWindow(int(self._codexWindowHandle))
			)
		except Exception:
			windowOpen = False
		if processRunning or windowOpen:
			return True
		self._codexWasFocused = False
		self._codexWindowHandle = None
		self._codexProcessId = None
		self._resetAutoReadBaseline()
		return False

	def _resetAutoReadBaseline(self):
		self._autoReadTaskOffsets = None
		self._autoReadLastReasoningByPath = {}
		self._knownAutoReadTaskPaths = set()
		self._autoReadTaskCacheBaselined = False

	def _monitoredActiveTasks(self):
		"""Return cached top-level, non-archived tasks with transcript paths."""
		now = time.monotonic()
		if (
			self._activeTaskMonitorRefreshedAt
			and now - self._activeTaskMonitorRefreshedAt < CODEX_ACTIVE_TASK_REFRESH_SECONDS
		):
			return self._activeTaskMonitorTasks
		self._activeTaskMonitorRefreshedAt = now
		try:
			tasks = _loadCodexTasks()
		except Exception:
			log.exception("Could not refresh active Codex tasks for auto-read")
			return self._activeTaskMonitorTasks
		monitored = []
		seenPaths = set()
		for task in tasks:
			path = task.get("rolloutPath") if isinstance(task, dict) else None
			if not isinstance(path, str) or not path.strip() or not os.path.isfile(path):
				path = _sessionPathForId(task.get("id") if isinstance(task, dict) else None)
			if not path or not os.path.isfile(path):
				continue
			key = _normalizePath(path)
			if key in seenPaths:
				continue
			seenPaths.add(key)
			entry = dict(task)
			entry["rolloutPath"] = path
			monitored.append(entry)
		self._activeTaskMonitorTasks = monitored
		return monitored

	def _readActiveTaskItems(self, fallbackPath=None):
		"""Incrementally read every active task with one stable offset per path.

		The manual navigation buffer and the most recently modified transcript do
		not affect these offsets. This prevents concurrent tasks from repeatedly
		re-baselining each other and dropping the record that changed file order.
		"""
		tasks = self._monitoredActiveTasks()
		available = {
			_normalizePath(task["rolloutPath"]): task
			for task in tasks
			if task.get("rolloutPath")
		}
		if fallbackPath and os.path.isfile(fallbackPath):
			fallbackKey = _normalizePath(fallbackPath)
			if fallbackKey not in available:
				metadata = _sessionMetadata(fallbackPath)
				cwd = metadata.get("cwd") if isinstance(metadata.get("cwd"), str) else ""
				available[fallbackKey] = {
					"id": metadata.get("id"),
					"title": "Current task",
					"project": _projectDisplayName(cwd) if cwd else "Standalone",
					"rolloutPath": fallbackPath,
				}
		previouslyKnown = set(self._knownAutoReadTaskPaths)
		self._knownAutoReadTaskPaths.update(available)
		if self._autoReadTaskOffsets is None:
			self._autoReadTaskOffsets = {}
			for key, task in available.items():
				try:
					self._autoReadTaskOffsets[key] = os.path.getsize(task["rolloutPath"])
				except OSError:
					pass
			self._autoReadTaskCacheBaselined = bool(tasks)
			return []

		if tasks and not self._autoReadTaskCacheBaselined:
			# The first task-cache load can arrive after a fallback transcript was
			# already baselined. Baseline the rest here instead of replaying every
			# historical message from all existing active tasks.
			for key, task in available.items():
				if key in self._autoReadTaskOffsets:
					continue
				try:
					self._autoReadTaskOffsets[key] = os.path.getsize(task["rolloutPath"])
				except OSError:
					pass
			self._autoReadTaskCacheBaselined = True

		result = []
		for key, task in available.items():
			path = task["rolloutPath"]
			if key not in self._autoReadTaskOffsets:
				if key in previouslyKnown:
					try:
						self._autoReadTaskOffsets[key] = os.path.getsize(path)
					except OSError:
						continue
					continue
				# A task that appears after the initial baseline is new active work;
				# read it from the beginning so its first activity is not missed.
				self._autoReadTaskOffsets[key] = 0
			offset = self._autoReadTaskOffsets[key]
			try:
				if os.path.getsize(path) == offset:
					continue
			except OSError:
				continue
			items, newOffset = _readTranscriptMessagesFromOffset(path, offset)
			self._autoReadTaskOffsets[key] = newOffset
			for item in items:
				if item["role"] == "reasoning":
					if item["text"] == self._autoReadLastReasoningByPath.get(key):
						continue
					self._autoReadLastReasoningByPath[key] = item["text"]
				result.append((task, item))
		return result

	def _announceAutoRead(self, text, urgent=False):
		"""Queue one independent NVDA utterance without losing later tasks."""
		priority = speech.Spri.NOW if urgent else speech.Spri.NEXT
		log.info(
			"Codex auto-read announcement queued: priority=%s, characters=%d",
			"NOW" if urgent else "NEXT",
			len(text),
		)
		try:
			ui.message(text, speechPriority=priority)
		except TypeError:
			# Retain compatibility with older supported NVDA releases whose
			# ui.message did not yet expose the speechPriority keyword.
			ui.message(text)

	def _checkTaskCompletions(self):
		"""Return the number of newly appended Codex final-answer records.

		Offsets are baselined on the first poll, so loading or restarting NVDA
		does not announce old completions. After that, only appended JSONL lines
		are parsed, allowing background tasks to notify NVDA without repeatedly
		reading every full transcript.
		"""
		paths = _sessionPaths()
		if self._completionOffsets is None:
			self._completionOffsets = {}
			for path in paths:
				try:
					self._completionOffsets[path] = os.path.getsize(path)
				except OSError:
					pass
			return 0

		finished = 0
		currentPaths = set(paths)
		for oldPath in tuple(self._completionOffsets):
			if oldPath not in currentPaths:
				del self._completionOffsets[oldPath]
		for path in paths:
			try:
				size = os.path.getsize(path)
			except OSError:
				continue
			offset = self._completionOffsets.get(path, 0)
			if size < offset:
				# Replaced or truncated file: establish a new baseline.
				self._completionOffsets[path] = size
				continue
			if size == offset:
				continue
			messages, newOffset = _readTranscriptMessagesFromOffset(path, offset)
			finished += sum(
				1 for message in messages
				if message["role"] == "assistant" and message.get("phase") == "final_answer"
			)
			self._completionOffsets[path] = newOffset
		return finished

	def _autoReadCheck(self):
		"""Read new Codex activity/messages and announce completed tasks."""
		if self._terminated:
			return
		if not self._autoReadFirstPollLogged:
			self._autoReadFirstPollLogged = True
			log.info("Codex auto-read monitor received its first timer callback")
		try:
			completedTasks = self._checkTaskCompletions()
			completionNotice = (
				"%d Codex tasks finished" % completedTasks
				if completedTasks > 1 else "Codex task finished"
			)
			activeTaskItems = self._readActiveTaskItems(self._resolveTranscriptPath())
			readFinals = sum(
				1 for _task, item in activeTaskItems
				if item["role"] == "assistant" and item.get("phase") == "final_answer"
			)
			taskFinished = completedTasks > 0 or readFinals > 0
			announcements = []
			if self._autoReadEnabled:
				pendingReasoning = {}
				for task, item in activeTaskItems:
					label = _taskListLabel(task)
					text = item["text"]
					if len(text) > CODEX_AUTO_READ_MAX_CHARS:
						text = text[:CODEX_AUTO_READ_MAX_CHARS].rstrip() + "..."
					if item["role"] == "reasoning":
						# Reasoning summaries are cumulative collapsed labels. Retain
						# only the newest pending label for each task so speech cannot
						# build an ever-growing activity backlog.
						pendingReasoning[_normalizePath(task.get("rolloutPath") or label)] = (
							"Codex activity in %s. %s" % (label, text)
						)
					elif item.get("phase") == "final_answer":
						announcements.append(("Codex task finished: %s. %s" % (label, text), True))
					elif item["role"] == "assistant":
						announcements.append(("Codex in %s. %s" % (label, text), False))
				for activity in pendingReasoning.values():
					announcements.append((activity, False))
				unmatchedCompletions = max(0, completedTasks - readFinals)
				if unmatchedCompletions:
					announcements.append((
						"%d Codex tasks finished" % unmatchedCompletions
						if unmatchedCompletions > 1 else "Codex task finished",
						True,
					))
			elif taskFinished:
				announcements.append((completionNotice, True))

			for text, urgent in announcements:
				self._announceAutoRead(text, urgent=urgent)
		except Exception:
			# A malformed/incomplete transcript must not permanently stop the
			# owned periodic timer or disable completion notifications.
			log.exception("Codex auto-read poll failed")

	def toggleAutoRead(self):
		self._autoReadEnabled = not self._autoReadEnabled
		ui.message("Codex auto-read %s" % ("on" if self._autoReadEnabled else "off"))

	def _refreshTranscript(self):
		path, messages = self._loadActiveTranscript()
		if not messages:
			self._transcriptPath = path
			self._messages = []
			self._index = None
			return False
		if path != self._transcriptPath or len(messages) != len(self._messages):
			self._transcriptPath = path
			self._messages = messages
			self._index = len(messages) - 1
		else:
			self._messages = messages
		return True

	def _requireCodex(self):
		if _isChatGPTForeground():
			return True
		ui.message("ChatGPT is not focused")
		return False

	# Backend facade used by the unified gesture dispatcher.
	def nextMessage(self):
		self._moveTranscript(1)

	def previousMessage(self):
		self._moveTranscript(-1)

	def firstMessage(self):
		self._speakTranscriptIndex(0)

	def lastMessage(self):
		self._speakTranscriptIndex(-1)

	def readCurrentMessage(self):
		self.script_currentCodexMessage(None)

	def openMenus(self):
		self.script_openCodexMenus(None)

	def isOpen(self):
		windowHandle, _appPath = _codexWindowDetails()
		return bool(windowHandle)

	def openSessionPicker(self):
		self.script_openCodexProjectPicker(None)

	def openTaskPicker(self):
		self.script_openCodexTaskPicker(None)

	def openChatPrompt(self):
		"""Open an NVDA-native prompt for the consumer Chat mode."""
		if not self._requireCodex():
			return
		if _findChatComposer() is None:
			ui.message("Open a ChatGPT Chat or Quick chat first")
			return
		if gui is None or getattr(gui, "mainFrame", None) is None:
			ui.message("The ChatGPT Chat prompt is unavailable because NVDA's user interface is not ready")
			return
		windowHandle = _topLevelWindowHandle()
		wx.CallAfter(self._showChatPrompt, windowHandle)

	def _showChatPrompt(self, windowHandle):
		style = wx.OK | wx.CANCEL | getattr(wx, "TE_MULTILINE", 0)
		dlg = wx.TextEntryDialog(
			gui.mainFrame,
			"Type the message NVDA should send through the current ChatGPT Chat.",
			"Send to ChatGPT Chat",
			"",
			style=style,
		)
		try:
			if dlg.ShowModal() != wx.ID_OK:
				return
			message = dlg.GetValue().strip()
		finally:
			dlg.Destroy()
		if not message:
			ui.message("Message is empty")
			return
		_restoreCodexFocus(windowHandle)
		wx.CallLater(150, self.sendChatMessage, message)

	def sendChatMessage(self, message, onComplete=None):
		"""Send one consumer-chat message and auto-read its completed response.

		``onComplete``, when supplied, receives ``(response, error)``. This
		makes the same path usable by another in-process NVDA helper without
		bypassing the signed-in ChatGPT application or its normal chat history.
		"""
		text = message.strip() if isinstance(message, str) else ""
		if not text:
			self._finishChatCallback(onComplete, None, "Message is empty")
			ui.message("Message is empty")
			return False
		root = _chatgptForegroundRoot()
		composer = _findChatComposer(root)
		if composer is None:
			error = "Open a ChatGPT Chat or Quick chat first"
			self._finishChatCallback(onComplete, None, error)
			ui.message(error)
			return False
		try:
			draft = getattr(composer, "value", "") or ""
		except Exception:
			draft = ""
		if isinstance(draft, str) and draft.strip():
			error = "The ChatGPT composer already contains a draft; clear or send it first"
			self._finishChatCallback(onComplete, None, error)
			ui.message(error)
			return False
		try:
			composer.setFocus()
		except Exception:
			error = "Could not focus the ChatGPT Chat composer"
			self._finishChatCallback(onComplete, None, error)
			ui.message(error)
			return False
		if not _sendUnicodeText(text):
			error = "Could not type the message into ChatGPT Chat"
			self._finishChatCallback(onComplete, None, error)
			ui.message(error)
			return False

		# React replaces the disabled Send button as input state updates. Give it
		# one event-loop turn, then activate the freshly exposed accessible button.
		time.sleep(0.08)
		root = _chatgptForegroundRoot() or root
		sendButton = _findChatSendButton(root)
		if sendButton is None:
			error = "ChatGPT did not expose its Send button"
			self._finishChatCallback(onComplete, None, error)
			ui.message(error)
			return False
		try:
			sendButton.doAction()
		except Exception:
			error = "Could not activate the ChatGPT Send button"
			self._finishChatCallback(onComplete, None, error)
			ui.message(error)
			return False

		self._chatRequestSerial += 1
		requestId = self._chatRequestSerial
		self._chatResponseLast = ""
		self._chatResponseStablePolls = 0
		self._chatActivityLast = ()
		self._chatRequestStartedAt = time.monotonic()
		ui.message("Message sent. Waiting for ChatGPT")
		wx.CallLater(700, self._pollChatResponse, requestId, text, onComplete)
		return True

	def _pollChatResponse(self, requestId, prompt, onComplete=None):
		if self._terminated or requestId != self._chatRequestSerial:
			return
		elapsed = time.monotonic() - self._chatRequestStartedAt
		if elapsed >= CHAT_RESPONSE_TIMEOUT_SECONDS:
			error = "Timed out waiting for the ChatGPT response"
			self._finishChatCallback(onComplete, None, error)
			ui.message(error)
			return
		root = _chatgptForegroundRoot()
		if root is None:
			wx.CallLater(CHAT_RESPONSE_POLL_MS, self._pollChatResponse, requestId, prompt, onComplete)
			return
		response, responding, activities = _chatResponseState(root, prompt)
		self._announceChatActivities(activities)
		if responding:
			self._chatResponseStablePolls = 0
		elif response:
			if response == self._chatResponseLast:
				self._chatResponseStablePolls += 1
			else:
				self._chatResponseLast = response
				self._chatResponseStablePolls = 0
			if elapsed >= 2.5 and self._chatResponseStablePolls >= 1:
				spoken = response if len(response) <= 12000 else response[:12000].rstrip() + "..."
				ui.message("ChatGPT response. %s" % spoken)
				self._finishChatCallback(onComplete, response, None)
				return
		else:
			self._chatResponseStablePolls = 0
		wx.CallLater(CHAT_RESPONSE_POLL_MS, self._pollChatResponse, requestId, prompt, onComplete)

	def _announceChatActivities(self, activities):
		"""Speak new ChatGPT progress cards once, without repeating each poll."""
		current = tuple(activity for activity in activities if isinstance(activity, str) and activity.strip())
		if current == self._chatActivityLast:
			return
		newActivities = [activity for activity in current if activity not in self._chatActivityLast]
		self._chatActivityLast = current
		if newActivities:
			ui.message("ChatGPT activity. %s" % "; ".join(newActivities))

	def _finishChatCallback(self, callback, response, error):
		if callback is None:
			return
		try:
			callback(response, error)
		except Exception:
			pass

	def nextSession(self):
		self._cycleThread(1)

	def previousSession(self):
		self._cycleThread(-1)

	def reportUsageLimits(self):
		requestId = self._nextUsageRequest()

		def worker():
			try:
				message = _readUsageSummary()
			except RuntimeError as e:
				message = _friendlyUsageError(e, "Could not read ChatGPT Codex usage limits")
			except Exception:
				message = "Could not read ChatGPT Codex usage limits"
			wx.CallAfter(self._finishUsageReport, requestId, message)

		self._startUsageWorker("CodexUsageLimitsReader", worker)

	def handleUsageCommand(self, repeatCount):
		"""Report usage on the first press and offer reset redemption on the second."""
		if repeatCount == 0:
			self.reportUsageLimits()
		elif repeatCount == 1:
			self.promptUsageReset()

	def promptUsageReset(self):
		requestId = self._nextUsageRequest()
		ui.message("Checking banked usage resets")

		def worker():
			try:
				state = _readUsageState()
				error = None
			except RuntimeError as e:
				state = None
				error = _friendlyUsageError(e, "Could not read banked usage resets")
			except Exception:
				state = None
				error = "Could not read banked usage resets"
			wx.CallAfter(self._finishUsageResetCheck, requestId, state, error)

		self._startUsageWorker("CodexUsageResetChecker", worker)

	def dumpDebug(self):
		self.script_codexDebugInfo(None)

	def terminate(self):
		self._terminated = True
		timer = self._autoReadTimer
		self._autoReadTimer = None
		if timer is not None:
			try:
				timer.Stop()
			except Exception:
				pass
		self._usageRequestSerial += 1
		self._chatRequestSerial += 1

	def _nextUsageRequest(self):
		self._usageRequestSerial += 1
		return self._usageRequestSerial

	def _isCurrentUsageRequest(self, requestId):
		return not self._terminated and requestId == self._usageRequestSerial

	def _startUsageWorker(self, name, target):
		thread = threading.Thread(target=target, name=name)
		thread.daemon = True
		thread.start()

	def _finishUsageReport(self, requestId, message):
		# A double press starts a newer reset-check request. Suppress the first
		# press's pending report so it does not speak over the confirmation.
		if self._isCurrentUsageRequest(requestId):
			ui.message(message)

	def _finishUsageResetCheck(self, requestId, state, error):
		if not self._isCurrentUsageRequest(requestId):
			return
		if error:
			ui.message(error)
			return
		resetCredits = state.get("resetCredits") if isinstance(state, dict) else None
		if not isinstance(resetCredits, dict):
			ui.message("Banked usage resets are unavailable for this account or Codex version")
			return
		availableCount = resetCredits.get("availableCount", 0)
		if availableCount <= 0:
			ui.message("No banked usage resets are available")
			return
		if gui is None or getattr(gui, "mainFrame", None) is None:
			ui.message("Banked usage reset confirmation is unavailable because NVDA's user interface is not ready")
			return

		credit = _preferredResetCredit(resetCredits)
		parts = [
			"You have %d banked usage %s." % (
				availableCount,
				"reset" if availableCount == 1 else "resets",
			),
			_resetScopeText(state.get("windows")),
		]
		if credit:
			title = credit.get("title")
			if isinstance(title, str) and title.strip():
				parts.append("The reset selected is %s." % title.strip())
			expires = _formatResetCreditExpiry(credit)
			if expires:
				parts.append("It expires %s." % expires)
		parts.append("Do you want to use one now?")
		style = wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION
		if gui.messageBox(" ".join(parts), "Use banked usage reset?", style) != wx.YES:
			return
		self._consumeUsageReset(credit.get("id") if credit else None)

	def _consumeUsageReset(self, creditId):
		requestId = self._nextUsageRequest()
		idempotencyKey = str(uuid.uuid4())
		ui.message("Using banked usage reset")

		def worker():
			try:
				response = _requestAppServerRateLimitReset(idempotencyKey, creditId)
				outcome = response.get("outcome")
				error = None
			except RuntimeError as e:
				outcome = None
				error = _friendlyUsageError(e, "Could not use the banked usage reset")
			except Exception:
				outcome = None
				error = "Could not use the banked usage reset"

			updatedSummary = None
			if outcome in ("reset", "alreadyRedeemed"):
				try:
					updatedSummary = _readUsageSummary()
				except Exception:
					pass
			wx.CallAfter(self._finishUsageReset, requestId, outcome, updatedSummary, error)

		self._startUsageWorker("CodexUsageResetConsumer", worker)

	def _finishUsageReset(self, requestId, outcome, updatedSummary, error):
		if not self._isCurrentUsageRequest(requestId):
			return
		if error:
			ui.message(error)
			return
		if outcome == "reset":
			message = "Banked usage reset used"
		elif outcome == "alreadyRedeemed":
			message = "The banked usage reset was already applied"
		elif outcome == "nothingToReset":
			ui.message("There is no eligible usage limit to reset")
			return
		elif outcome == "noCredit":
			ui.message("No banked usage reset is available")
			return
		else:
			ui.message("Codex returned an unknown banked usage reset result")
			return
		if updatedSummary:
			message += ". " + updatedSummary
		else:
			message += ". Usage limits could not be refreshed"
		ui.message(message)

	def script_openCodexMenus(self, gesture):
		"""Open an accessible mirror of the ChatGPT desktop menus."""
		if not self._requireCodex():
			return
		if gui is None or getattr(gui, "mainFrame", None) is None:
			ui.message("ChatGPT menus are unavailable because NVDA's user interface is not ready")
			return
		windowHandle = _topLevelWindowHandle()
		wx.CallAfter(self._showMenu, windowHandle)

	def script_openCodexProjectPicker(self, gesture):
		"""Open an accessible mirror of ChatGPT Codex projects and tasks."""
		if gui is None or getattr(gui, "mainFrame", None) is None:
			ui.message("ChatGPT Codex projects are unavailable because NVDA's user interface is not ready")
			return
		windowHandle, codexPath = _codexWindowDetails()
		codexPath = codexPath or _codexExecutablePath()
		if not codexPath:
			ui.message("ChatGPT Desktop is not installed")
			return
		wx.CallAfter(self._showProjectPicker, windowHandle, codexPath)

	def script_openCodexTaskPicker(self, gesture):
		"""Open the accessible list of active, top-level Codex tasks."""
		if gui is None or getattr(gui, "mainFrame", None) is None:
			ui.message("ChatGPT Codex tasks are unavailable because NVDA's user interface is not ready")
			return
		windowHandle, codexPath = _codexWindowDetails()
		codexPath = codexPath or _codexExecutablePath()
		if not codexPath:
			ui.message("ChatGPT Desktop is not installed")
			return
		wx.CallAfter(self._showTaskPicker, windowHandle, codexPath)

	def _showMenu(self, windowHandle):
		menu = wx.Menu()
		handlers = []
		for sectionName, items in MENU_SECTIONS:
			submenu = wx.Menu()
			for itemName, gestureName in items:
				item = submenu.Append(wx.ID_ANY, itemName)
				if gestureName is None:
					item.Enable(False)
				else:
					handler = self._menuHandler(windowHandle, gestureName, itemName)
					gui.mainFrame.Bind(wx.EVT_MENU, handler, item)
					handlers.append((item, handler))
			menu.AppendSubMenu(submenu, sectionName)
		try:
			gui.mainFrame.PopupMenu(menu)
		finally:
			for item, handler in handlers:
				try:
					gui.mainFrame.Unbind(wx.EVT_MENU, id=item.GetId(), handler=handler)
				except Exception:
					pass
			menu.Destroy()

	def _showProjectPicker(self, windowHandle, codexPath=None):
		dlg = _ProjectThreadDialog(
			gui.mainFrame,
			windowHandle,
			codexPath=codexPath,
			onProjectOpened=self._activateProject,
			onTaskOpened=self._activateTask,
		)
		try:
			dlg.ShowModal()
		finally:
			dlg.Destroy()

	def _showTaskPicker(self, windowHandle, codexPath=None):
		dlg = _TaskDialog(
			gui.mainFrame,
			windowHandle=windowHandle,
			codexPath=codexPath,
			onTaskOpened=self._activateTask,
		)
		try:
			dlg.ShowModal()
		finally:
			dlg.Destroy()

	def _menuHandler(self, windowHandle, gestureName, itemName):
		def handler(evt):
			_restoreCodexFocus(windowHandle)
			wx.CallLater(120, _sendGesture, gestureName, itemName)

		return handler

	def script_nextCodexMessage(self, gesture):
		"""Move to the next Codex transcript message."""
		self._moveTranscript(1)

	def script_previousCodexMessage(self, gesture):
		"""Move to the previous Codex transcript message."""
		self._moveTranscript(-1)

	def script_currentCodexMessage(self, gesture):
		"""Read the current Codex transcript message."""
		if not self._refreshTranscript():
			ui.message("No Codex transcript messages found")
			return
		if self._index is None:
			self._index = len(self._messages) - 1
		ui.message(_summarize(self._messages[self._index]))

	def script_codexDebugInfo(self, gesture):
		"""Report Codex transcript debug information."""
		if not self._requireCodex():
			return
		path, messages = _loadTranscript()
		if not messages:
			ui.message("No Codex transcript messages found")
			return
		ui.message("Transcript has %d messages. Latest: %s" % (len(messages), _summarize(messages[-1], limit=500)))

	def script_reportCodexUsageLimits(self, gesture):
		"""Report current Codex account usage limits."""
		self.reportUsageLimits()

	def _moveTranscript(self, offset):
		if not self._refreshTranscript():
			ui.message("No Codex transcript messages found")
			return
		if self._index is None:
			self._index = 0 if offset > 0 else len(self._messages) - 1
		else:
			self._index = max(0, min(len(self._messages) - 1, self._index + offset))
		ui.message(_summarize(self._messages[self._index]))

	def _speakTranscriptIndex(self, index):
		if not self._refreshTranscript():
			ui.message("No Codex transcript messages found")
			return
		self._index = index if index >= 0 else len(self._messages) - 1
		ui.message(_summarize(self._messages[self._index]))

	def _cycleThread(self, offset):
		codexFocused = _isChatGPTForeground()
		tasks = _taskCycleCandidates(_loadCodexTasks())
		if not tasks:
			ui.message("No active or recently used ChatGPT Codex tasks found")
			return
		if codexFocused:
			# Re-read the actual task shown in the app on every press.  The task list
			# can reorder as work arrives, and users can switch tasks without using
			# this add-on, so a remembered numeric index is not authoritative.
			currentTaskId = _currentVisibleCodexTaskId(tasks) or self._activeTaskId
		else:
			# Background cycling is deliberately an NVDA-only buffer operation.
			currentTaskId = self._activeTaskId
		if not currentTaskId:
			currentTaskId = _sessionMetadata(_latestSessionPath()).get("id")
		currentIndex = next(
			(index for index, item in enumerate(tasks) if item.get("id") == currentTaskId),
			None,
		)
		if currentIndex is None:
			self._threadCycleIndex = 0 if offset > 0 else len(tasks) - 1
		else:
			self._threadCycleIndex = (currentIndex + offset) % len(tasks)
		task = tasks[self._threadCycleIndex]
		if codexFocused:
			if not _launchCodexUrl("codex://threads/%s" % quote(task["id"], safe="")):
				ui.message("Could not open the selected ChatGPT Codex task")
				return
		self._activateTask(task)
		ui.message("[%d/%d] %s, %s%s" % (
			self._threadCycleIndex + 1,
			len(tasks),
			task["title"],
			task["project"],
			"" if codexFocused else ", NVDA buffer only",
		))


# Keep the original public class name for the copied upstream regression
# suite.  This file lives below globalPlugins/addtl, so NVDA will not load it
# as an independent global plugin.
GlobalPlugin = ChatGPTBackend
