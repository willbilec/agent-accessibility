import importlib.util
import json
import os
import queue
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "addon" / "globalPlugins" / "addtl" / "chatgptBackend.py"
DATABASE_HELPER_PATH = PLUGIN_PATH.with_name("chatgptDb.py")


def _load_plugin_module():
	module_name = "codexAccessibility_test_module"
	if module_name in sys.modules:
		return sys.modules[module_name]

	api = types.ModuleType("api")
	api.getForegroundObject = lambda: types.SimpleNamespace(
		appModule=types.SimpleNamespace(
			appPath=r"C:\Program Files\WindowsApps\OpenAI.Codex_26.707.8479.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe"
		),
		windowHandle=1,
	)
	api.getFocusObject = lambda: None
	sys.modules["api"] = api

	globalPluginHandler = types.ModuleType("globalPluginHandler")
	globalPluginHandler.GlobalPlugin = type("GlobalPlugin", (), {})
	sys.modules["globalPluginHandler"] = globalPluginHandler

	keyboardHandler = types.ModuleType("keyboardHandler")
	keyboardHandler.KeyboardInputGesture = type(
		"KeyboardInputGesture",
		(),
		{"fromName": staticmethod(lambda _name: types.SimpleNamespace(send=lambda: None))},
	)
	sys.modules["keyboardHandler"] = keyboardHandler

	speech = types.ModuleType("speech")
	speech.Spri = types.SimpleNamespace(NEXT=1, NOW=2)
	sys.modules["speech"] = speech

	ui = types.ModuleType("ui")
	ui.message = lambda _text: None
	sys.modules["ui"] = ui

	logHandler = types.ModuleType("logHandler")
	logHandler.log = types.SimpleNamespace(
		exception=lambda *_args, **_kwargs: None,
		info=lambda *_args, **_kwargs: None,
	)
	sys.modules["logHandler"] = logHandler

	winUser = types.ModuleType("winUser")
	winUser.WS_CAPTION = 1
	winUser.getWindowStyle = lambda _handle: 1
	winUser.setForegroundWindow = lambda _handle: None
	sys.modules["winUser"] = winUser

	wx = types.ModuleType("wx")
	wx.Dialog = type("Dialog", (), {})
	wx.DEFAULT_DIALOG_STYLE = 0
	wx.RESIZE_BORDER = 0
	wx.VERTICAL = 0
	wx.HORIZONTAL = 0
	wx.LB_SINGLE = 0
	wx.LB_NEEDED_SB = 0
	wx.BOTTOM = 0
	wx.EXPAND = 0
	wx.RIGHT = 0
	wx.LEFT = 0
	wx.ALL = 0
	wx.ID_CANCEL = 0
	wx.ID_OK = 1
	wx.YES = 2
	wx.NO = 4
	wx.YES_NO = 8
	wx.NO_DEFAULT = 16
	wx.ICON_QUESTION = 32
	wx.EVT_LISTBOX = object()
	wx.EVT_LISTBOX_DCLICK = object()
	wx.EVT_KEY_DOWN = object()
	wx.EVT_BUTTON = object()
	wx.EVT_CHAR_HOOK = object()
	wx.EVT_ACTIVATE = object()
	wx.EVT_MENU = object()
	wx.CallLater = lambda *_args, **_kwargs: None
	wx.CallAfter = lambda *_args, **_kwargs: None
	class FakeTimer:
		def __init__(self):
			self.interval = None
			self.started = False
			self.stopped = False

		def Start(self, interval):
			self.interval = interval
			self.started = True

		def Stop(self):
			self.stopped = True

	wx.Timer = FakeTimer
	wx.Menu = type("Menu", (), {})
	wx.BoxSizer = type("BoxSizer", (), {})
	wx.StaticText = type("StaticText", (), {})
	wx.ListBox = type("ListBox", (), {})
	wx.Button = type("Button", (), {})
	wx.DirDialog = type("DirDialog", (), {})
	wx.TextEntryDialog = type("TextEntryDialog", (), {})
	sys.modules["wx"] = wx

	gui = types.ModuleType("gui")
	gui.mainFrame = object()
	gui.messageBox = lambda *_args, **_kwargs: wx.NO
	sys.modules["gui"] = gui

	spec = importlib.util.spec_from_file_location(module_name, PLUGIN_PATH)
	module = importlib.util.module_from_spec(spec)
	sys.modules[module_name] = module
	spec.loader.exec_module(module)
	return module


class UsageLimitsTests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.plugin = _load_plugin_module()

	def test_backend_imports_when_nvda_omits_sqlite_extension(self):
		module_name = "codexAccessibility_test_module_without_sqlite"
		with mock.patch.dict(sys.modules, {"sqlite3": None}):
			spec = importlib.util.spec_from_file_location(module_name, PLUGIN_PATH)
			module = importlib.util.module_from_spec(spec)
			sys.modules[module_name] = module
			spec.loader.exec_module(module)
			self.assertIsNone(module.sqlite3)

	def test_foreground_detection_accepts_renamed_chatgpt_executable(self):
		foreground = types.SimpleNamespace(
			appModule=types.SimpleNamespace(appPath=r"C:\Program Files\WindowsApps\OpenAI.Codex_1.0\app\ChatGPT.exe")
		)
		with mock.patch.object(self.plugin.api, "getForegroundObject", return_value=foreground):
			self.assertTrue(self.plugin._isChatGPTForeground())

	def test_chat_response_extraction_uses_consumer_composer_boundary(self):
		def node(role, name=""):
			return types.SimpleNamespace(role=role, name=name, firstChild=None, next=None)

		root = node("window")
		prompt = node("staticText", "Tell me a joke")
		answer_one = node("heading", "A short joke")
		answer_two = node("staticText", "Why did the screen reader cross the road?")
		composer = node("editableText", self.plugin.CHAT_COMPOSER_NAME)
		root.firstChild = prompt
		prompt.next = answer_one
		answer_one.next = answer_two
		answer_two.next = composer

		response, responding = self.plugin._chatResponseText(root, "Tell me a joke")

		self.assertFalse(responding)
		self.assertEqual(response, "A short joke\nWhy did the screen reader cross the road?")

	def test_chat_response_extraction_detects_stop_button(self):
		def node(role, name=""):
			return types.SimpleNamespace(role=role, name=name, firstChild=None, next=None)

		root = node("window")
		prompt = node("staticText", "Hello")
		partial = node("staticText", "Hello there")
		stop = node("button", "Stop")
		composer = node("editableText", self.plugin.CHAT_COMPOSER_NAME)
		root.firstChild = prompt
		prompt.next = partial
		partial.next = stop
		stop.next = composer

		response, responding = self.plugin._chatResponseText(root, "Hello")

		self.assertTrue(responding)
		self.assertEqual(response, "Hello there")

	def test_chat_response_activity_is_announced_separately_from_answer(self):
		def node(role, name=""):
			return types.SimpleNamespace(role=role, name=name, firstChild=None, next=None)

		root = node("window")
		prompt = node("staticText", "Find an accessible restaurant")
		activity = node("staticText", "Implementing deduplicated chat activity announcements")
		answer = node("staticText", "Here are three nearby options.")
		composer = node("editableText", self.plugin.CHAT_COMPOSER_NAME)
		root.firstChild = prompt
		prompt.next = activity
		activity.next = answer
		answer.next = composer

		response, responding, activities = self.plugin._chatResponseState(root, "Find an accessible restaurant")

		self.assertFalse(responding)
		self.assertEqual(response, "Here are three nearby options.")
		self.assertEqual(activities, ["Implementing deduplicated chat activity announcements"])

	def test_action_worded_user_prompt_is_not_mistaken_for_chat_activity(self):
		def node(role, name=""):
			return types.SimpleNamespace(role=role, name=name, firstChild=None, next=None)

		promptText = "Implementing an accessible ChatGPT reader"
		root = node("window")
		prompt = node("staticText", promptText)
		answer = node("staticText", "That is a useful accessibility improvement.")
		composer = node("editableText", self.plugin.CHAT_COMPOSER_NAME)
		root.firstChild = prompt
		prompt.next = answer
		answer.next = composer

		response, _responding, activities = self.plugin._chatResponseState(root, promptText)

		self.assertEqual(response, "That is a useful accessibility improvement.")
		self.assertEqual(activities, [])

	def test_chat_activity_announcements_are_de_duplicated_between_polls(self):
		backend = self.plugin.ChatGPTBackend()
		messages = []
		with mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
			backend._announceChatActivities(["Searching the web"])
			backend._announceChatActivities(["Searching the web"])
			backend._announceChatActivities(["Reading sources"])

		self.assertEqual(messages, [
			"ChatGPT activity. Searching the web",
			"ChatGPT activity. Reading sources",
		])

	def test_codex_auto_read_baselines_then_speaks_new_assistant_messages(self):
		backend = self.plugin.ChatGPTBackend()
		task = {"title": "Current task", "project": "Accessibility"}
		new = {"role": "assistant", "text": "New response"}
		messages = []
		with mock.patch.object(self.plugin, "_isChatGPTForeground", return_value=True), \
			mock.patch.object(backend, "_checkTaskCompletions", return_value=0), \
			mock.patch.object(backend, "_resolveTranscriptPath", return_value="task.jsonl"), \
			mock.patch.object(backend, "_readActiveTaskItems", side_effect=[[], [(task, new)]]), \
			mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
			backend._autoReadCheck()
			backend._autoReadCheck()

		self.assertEqual(messages, ["Codex in Current task — Accessibility. New response"])

	def test_codex_monitor_owns_periodic_timer_until_termination(self):
		backend = self.plugin.ChatGPTBackend()
		timer = backend._autoReadTimer

		self.assertIsNotNone(timer)
		self.assertTrue(timer.started)
		self.assertEqual(timer.interval, self.plugin.CODEX_AUTO_READ_POLL_MS)
		callback = mock.Mock()
		timer._callback = callback
		timer.Notify()
		callback.assert_called_once_with()

		backend.terminate()
		self.assertTrue(timer.stopped)
		self.assertIsNone(backend._autoReadTimer)

	def test_codex_auto_read_continues_outside_window_until_codex_closes(self):
		backend = self.plugin.ChatGPTBackend()
		task = {"title": "Background task", "project": "Accessibility"}
		away = {"role": "assistant", "text": "Response while away", "phase": "commentary"}
		messages = []
		with mock.patch.object(self.plugin, "_isChatGPTForeground", side_effect=[True, False, False]), \
			mock.patch.object(self.plugin, "_topLevelWindowHandle", return_value=123), \
			mock.patch.object(self.plugin, "_windowProcessId", return_value=None), \
			mock.patch.object(self.plugin, "_isProcessRunning", return_value=False), \
			mock.patch.object(self.plugin.ctypes.windll.user32, "IsWindow", side_effect=[True, False]), \
			mock.patch.object(backend, "_checkTaskCompletions", return_value=0), \
			mock.patch.object(backend, "_resolveTranscriptPath", return_value="task.jsonl"), \
			mock.patch.object(backend, "_readActiveTaskItems", side_effect=[[], [(task, away)], []]), \
			mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
			backend._autoReadCheck()
			backend._autoReadCheck()
			backend._autoReadCheck()

		self.assertEqual(messages, ["Codex in Background task — Accessibility. Response while away"])
		self.assertFalse(backend._codexWasFocused)

	def test_codex_auto_read_announces_activity_from_other_active_tasks(self):
		backend = self.plugin.ChatGPTBackend()
		task = {
			"id": "background-task",
			"title": "Fix active task monitoring",
			"project": "Accessibility",
			"rolloutPath": "background.jsonl",
		}
		activity = {"role": "reasoning", "text": "Running background checks", "phase": "reasoning"}
		commentary = {"role": "assistant", "text": "One check remains", "phase": "commentary"}
		messages = []
		with mock.patch.object(self.plugin, "_isChatGPTForeground", return_value=True), \
			mock.patch.object(self.plugin, "_topLevelWindowHandle", return_value=123), \
			mock.patch.object(backend, "_checkTaskCompletions", return_value=0), \
			mock.patch.object(backend, "_resolveTranscriptPath", return_value="selected.jsonl"), \
			mock.patch.object(backend, "_readActiveTaskItems", return_value=[
				(task, activity),
				(task, commentary),
			]), \
			mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
			backend._autoReadCheck()

		self.assertEqual(messages, [
			"Codex in Fix active task monitoring — Accessibility. One check remains",
			"Codex activity in Fix active task monitoring — Accessibility. Running background checks",
		])

	def test_codex_auto_read_has_no_foreground_gate(self):
		backend = self.plugin.ChatGPTBackend()
		task = {"title": "Other task", "project": "P"}
		item = {"role": "assistant", "text": "Read globally", "phase": "commentary"}
		messages = []
		with mock.patch.object(self.plugin, "_isChatGPTForeground", return_value=False), \
			mock.patch.object(backend, "_checkTaskCompletions", return_value=0), \
			mock.patch.object(backend, "_resolveTranscriptPath", return_value=None), \
			mock.patch.object(backend, "_readActiveTaskItems", return_value=[(task, item)]), \
			mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
			backend._autoReadCheck()

		self.assertEqual(messages, ["Codex in Other task — P. Read globally"])

	def test_codex_auto_read_uses_nvda_speech_priorities(self):
		backend = self.plugin.ChatGPTBackend()
		with mock.patch.object(self.plugin.ui, "message") as message:
			backend._announceAutoRead("Activity")
			backend._announceAutoRead("Finished", urgent=True)

		self.assertEqual(message.call_args_list, [
			mock.call("Activity", speechPriority=self.plugin.speech.Spri.NEXT),
			mock.call("Finished", speechPriority=self.plugin.speech.Spri.NOW),
		])

	def test_most_recent_path_switches_do_not_drop_concurrent_task_messages(self):
		backend = self.plugin.ChatGPTBackend()
		with tempfile.TemporaryDirectory() as temp_dir:
			path_a = os.path.join(temp_dir, "a.jsonl")
			path_b = os.path.join(temp_dir, "b.jsonl")
			for task_id, path in (("a", path_a), ("b", path_b)):
				with open(path, "w", encoding="utf-8") as session_file:
					session_file.write(json.dumps({"type": "session_meta", "payload": {"id": task_id}}) + "\n")
			task_a = {"id": "a", "title": "Task A", "project": "P", "rolloutPath": path_a}
			task_b = {"id": "b", "title": "Task B", "project": "P", "rolloutPath": path_b}
			with mock.patch.object(backend, "_monitoredActiveTasks", return_value=[task_a, task_b]):
				self.assertEqual(backend._readActiveTaskItems(path_a), [])
				with open(path_a, "a", encoding="utf-8") as session_file:
					session_file.write(json.dumps({
						"type": "response_item",
						"payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": "A one"}]},
					}) + "\n")
				self.assertEqual(backend._readActiveTaskItems(path_a)[0][1]["text"], "A one")
				with open(path_b, "a", encoding="utf-8") as session_file:
					session_file.write(json.dumps({
						"type": "response_item",
						"payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": "B one"}]},
					}) + "\n")
				self.assertEqual(backend._readActiveTaskItems(path_b)[0][1]["text"], "B one")
				with open(path_a, "a", encoding="utf-8") as session_file:
					session_file.write(json.dumps({
						"type": "response_item",
						"payload": {"type": "message", "role": "assistant", "phase": "commentary", "content": [{"type": "output_text", "text": "A two"}]},
					}) + "\n")
				self.assertEqual(backend._readActiveTaskItems(path_a)[0][1]["text"], "A two")

	def test_codex_auto_read_speaks_while_away_and_does_not_replay_on_return(self):
		backend = self.plugin.ChatGPTBackend()
		task = {"title": "Task", "project": "P"}
		new = {"role": "assistant", "text": "Response while away"}
		messages = []
		with mock.patch.object(self.plugin, "_isChatGPTForeground", side_effect=[True, False, True]), \
			mock.patch.object(self.plugin, "_windowProcessId", return_value=456), \
			mock.patch.object(self.plugin, "_isProcessRunning", return_value=True), \
			mock.patch.object(self.plugin.ctypes.windll.user32, "IsWindow", return_value=True), \
			mock.patch.object(backend, "_checkTaskCompletions", return_value=0), \
			mock.patch.object(backend, "_resolveTranscriptPath", return_value="task.jsonl"), \
			mock.patch.object(backend, "_readActiveTaskItems", side_effect=[[], [(task, new)], []]), \
			mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
			backend._autoReadCheck()
			backend._autoReadCheck()
			backend._autoReadCheck()

		self.assertEqual(messages, ["Codex in Task — P. Response while away"])

	def test_codex_auto_read_toggle_announces_state(self):
		backend = self.plugin.ChatGPTBackend()
		messages = []
		with mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
			backend.toggleAutoRead()
			backend.toggleAutoRead()

		self.assertEqual(messages, ["Codex auto-read off", "Codex auto-read on"])

	def test_codex_task_completion_notifies_once_when_auto_read_is_off(self):
		backend = self.plugin.ChatGPTBackend()
		backend._autoReadEnabled = False
		task = {"title": "Task", "project": "P"}
		finished = {"role": "assistant", "text": "Done", "phase": "final_answer"}
		messages = []
		with mock.patch.object(self.plugin, "_isChatGPTForeground", return_value=False), \
			mock.patch.object(backend, "_checkTaskCompletions", return_value=0), \
			mock.patch.object(backend, "_resolveTranscriptPath", return_value="task.jsonl"), \
			mock.patch.object(backend, "_readActiveTaskItems", side_effect=[[(task, finished)], [], []]), \
			mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
			backend._autoReadCheck()
			backend._autoReadCheck()
			backend._autoReadCheck()

		self.assertEqual(messages, ["Codex task finished"])

	def test_codex_task_completion_and_final_response_use_one_announcement(self):
		backend = self.plugin.ChatGPTBackend()
		task = {"title": "Task", "project": "P"}
		finished = {"role": "assistant", "text": "All fixed", "phase": "final_answer"}
		messages = []
		with mock.patch.object(self.plugin, "_isChatGPTForeground", return_value=True), \
			mock.patch.object(backend, "_checkTaskCompletions", return_value=0), \
			mock.patch.object(backend, "_resolveTranscriptPath", return_value="task.jsonl"), \
			mock.patch.object(backend, "_readActiveTaskItems", side_effect=[[], [(task, finished)]]), \
			mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
			backend._autoReadCheck()
			backend._autoReadCheck()

		self.assertEqual(messages, ["Codex task finished: Task — P. All fixed"])

	def test_codex_auto_read_speaks_collapsed_reasoning_summary_without_opening_ui(self):
		backend = self.plugin.ChatGPTBackend()
		task = {"title": "Task", "project": "P"}
		activity = {"role": "reasoning", "text": "Integrating transcript reader", "phase": "reasoning"}
		messages = []
		with mock.patch.object(self.plugin, "_isChatGPTForeground", return_value=True), \
			mock.patch.object(backend, "_checkTaskCompletions", return_value=0), \
			mock.patch.object(backend, "_resolveTranscriptPath", return_value="task.jsonl"), \
			mock.patch.object(backend, "_readActiveTaskItems", side_effect=[
				[(task, activity)],
				[],
			]), \
			mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
			backend._autoReadCheck()
			backend._autoReadCheck()

		self.assertEqual(messages, ["Codex activity in Task — P. Integrating transcript reader"])

	def test_reasoning_reader_uses_latest_collapsed_summary_label(self):
		payload = {
			"summary": [
				{"type": "summary_text", "text": "**Earlier activity**"},
				{"type": "summary_text", "text": "**Integrating _readTranscriptMessagesFromOffset into completion monitor**"},
			]
		}
		self.assertEqual(
			self.plugin._reasoningSummaryText(payload),
			"Integrating _readTranscriptMessagesFromOffset into completion monitor",
		)

	def test_completion_monitor_notifies_for_background_task_once(self):
		backend = self.plugin.ChatGPTBackend()
		with tempfile.TemporaryDirectory() as temp_dir:
			path = os.path.join(temp_dir, "rollout-background.jsonl")
			with open(path, "w", encoding="utf-8") as session_file:
				session_file.write(json.dumps({"type": "session_meta", "payload": {"id": "background"}}) + "\n")
			with mock.patch.object(self.plugin, "SESSIONS_DIR", temp_dir):
				self.assertEqual(backend._checkTaskCompletions(), 0)
				with open(path, "a", encoding="utf-8") as session_file:
					session_file.write(json.dumps({
						"type": "response_item",
						"payload": {
							"type": "message",
							"role": "assistant",
							"phase": "final_answer",
							"content": [{"type": "output_text", "text": "Done"}],
						},
					}) + "\n")
				self.assertEqual(backend._checkTaskCompletions(), 1)
				self.assertEqual(backend._checkTaskCompletions(), 0)

	def test_incremental_transcript_reader_retries_partial_reasoning_line(self):
		with tempfile.TemporaryDirectory() as temp_dir:
			path = os.path.join(temp_dir, "partial.jsonl")
			with open(path, "w", encoding="utf-8") as session_file:
				session_file.write(json.dumps({"type": "session_meta", "payload": {"id": "partial"}}) + "\n")
			offset = os.path.getsize(path)
			record = json.dumps({
				"type": "response_item",
				"payload": {
					"type": "reasoning",
					"summary": [{"type": "summary_text", "text": "**Checking the fix**"}],
				},
			})
			split = len(record) // 2
			with open(path, "a", encoding="utf-8") as session_file:
				session_file.write(record[:split])

			items, retry_offset = self.plugin._readTranscriptMessagesFromOffset(path, offset)
			self.assertEqual(items, [])
			self.assertEqual(retry_offset, offset)

			with open(path, "a", encoding="utf-8") as session_file:
				session_file.write(record[split:] + "\n")
			items, final_offset = self.plugin._readTranscriptMessagesFromOffset(path, retry_offset)
			self.assertEqual(items, [{"role": "reasoning", "text": "Checking the fix", "phase": "reasoning"}])
			self.assertEqual(final_offset, os.path.getsize(path))

	def test_activating_task_retargets_transcript_buffer(self):
		backend = self.plugin.ChatGPTBackend()
		task = {"id": "target-task", "rolloutPath": "target.jsonl"}
		loaded = [{"role": "assistant", "text": "Target response"}]
		with mock.patch.object(self.plugin.os.path, "isfile", return_value=True), \
			mock.patch.object(self.plugin, "_loadTranscriptPath", return_value=("target.jsonl", loaded)) as load:
			backend._activateTask(task)
			self.assertTrue(backend._refreshTranscript())

		load.assert_called_once_with("target.jsonl")
		self.assertEqual(backend._activeTaskId, "target-task")
		self.assertEqual(backend._messages, loaded)

	def test_task_cycle_candidates_keep_only_last_seven_days(self):
		now = 2000000000.0
		tasks = [
			{"id": "new-ms", "updatedAt": int((now - 60) * 1000)},
			{"id": "boundary", "updatedAt": now - self.plugin.CODEX_TASK_CYCLE_MAX_AGE_SECONDS},
			{"id": "old", "updatedAt": now - self.plugin.CODEX_TASK_CYCLE_MAX_AGE_SECONDS - 1},
		]

		self.assertEqual(
			[task["id"] for task in self.plugin._taskCycleCandidates(tasks, now=now)],
			["new-ms", "boundary"],
		)

	def test_foreground_task_cycle_opens_task_and_retargets_buffer(self):
		backend = self.plugin.ChatGPTBackend()
		backend._activeTaskId = "current"
		now = self.plugin.time.time()
		tasks = [
			{"id": "newest", "title": "Newest", "project": "One", "rolloutPath": "newest.jsonl", "updatedAt": now},
			{"id": "current", "title": "Current", "project": "One", "rolloutPath": "current.jsonl", "updatedAt": now},
			{"id": "next", "title": "Next", "project": "Two", "rolloutPath": "next.jsonl", "updatedAt": now},
		]
		with mock.patch.object(self.plugin, "_isChatGPTForeground", return_value=True), \
			mock.patch.object(self.plugin, "_loadCodexTasks", return_value=tasks), \
			mock.patch.object(self.plugin, "_currentVisibleCodexTaskId", return_value="current"), \
			mock.patch.object(self.plugin, "_launchCodexUrl", return_value=True) as launch, \
			mock.patch.object(self.plugin.os.path, "isfile", return_value=True), \
			mock.patch.object(self.plugin.ui, "message"):
			backend._cycleThread(1)

		launch.assert_called_once_with("codex://threads/next")
		self.assertEqual(backend._activeTaskId, "next")
		self.assertEqual(backend._activeTranscriptPath, "next.jsonl")

	def test_foreground_task_cycle_uses_live_task_instead_of_stale_buffer(self):
		backend = self.plugin.ChatGPTBackend()
		backend._activeTaskId = "stale"
		now = self.plugin.time.time()
		tasks = [
			{"id": "stale", "title": "Stale", "project": "One", "rolloutPath": "stale.jsonl", "updatedAt": now},
			{"id": "visible", "title": "Visible", "project": "One", "rolloutPath": "visible.jsonl", "updatedAt": now},
			{"id": "next", "title": "Next", "project": "Two", "rolloutPath": "next.jsonl", "updatedAt": now},
		]
		with mock.patch.object(self.plugin, "_isChatGPTForeground", return_value=True), \
			mock.patch.object(self.plugin, "_loadCodexTasks", return_value=tasks), \
			mock.patch.object(self.plugin, "_currentVisibleCodexTaskId", return_value="visible") as visible, \
			mock.patch.object(self.plugin, "_launchCodexUrl", return_value=True) as launch, \
			mock.patch.object(self.plugin.os.path, "isfile", return_value=True), \
			mock.patch.object(self.plugin.ui, "message"):
			backend._cycleThread(1)

		visible.assert_called_once_with(tasks)
		launch.assert_called_once_with("codex://threads/next")
		self.assertEqual(backend._activeTaskId, "next")

	def test_live_task_detection_ignores_sidebar_and_uses_content_title(self):
		def node(role, name="", parent=None):
			return types.SimpleNamespace(
				role=role,
				name=name,
				parent=parent,
				firstChild=None,
				next=None,
			)

		root = node("document", "Codex")
		sidebar = node("list", "Tasks", root)
		sidebarTitle = node("button", "Other task", sidebar)
		content = node("group", "", root)
		contentTitle = node("button", "Current task", content)
		root.firstChild = sidebar
		sidebar.next = content
		sidebar.firstChild = sidebarTitle
		content.firstChild = contentTitle
		tasks = [
			{"id": "other", "title": "Other task"},
			{"id": "current", "title": "Current task"},
		]
		with mock.patch.object(self.plugin, "_chatgptForegroundRoot", return_value=root):
			self.assertEqual(self.plugin._currentVisibleCodexTaskId(tasks), "current")

	def test_background_task_cycle_only_retargets_nvda_buffer(self):
		backend = self.plugin.ChatGPTBackend()
		backend._activeTaskId = "current"
		now = self.plugin.time.time()
		tasks = [
			{"id": "current", "title": "Current", "project": "One", "rolloutPath": "current.jsonl", "updatedAt": now},
			{"id": "next", "title": "Next", "project": "Two", "rolloutPath": "next.jsonl", "updatedAt": now},
		]
		messages = []
		with mock.patch.object(self.plugin, "_isChatGPTForeground", return_value=False), \
			mock.patch.object(self.plugin, "_loadCodexTasks", return_value=tasks), \
			mock.patch.object(self.plugin, "_launchCodexUrl") as launch, \
			mock.patch.object(self.plugin.os.path, "isfile", return_value=True), \
			mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
			backend._cycleThread(1)

		launch.assert_not_called()
		self.assertEqual(backend._activeTaskId, "next")
		self.assertEqual(backend._activeTranscriptPath, "next.jsonl")
		self.assertEqual(messages, ["[2/2] Next, Two, NVDA buffer only"])

	def test_transcript_navigation_does_not_require_chatgpt_focus(self):
		backend = self.plugin.ChatGPTBackend()
		loaded = [
			{"role": "user", "text": "Question"},
			{"role": "assistant", "text": "Answer"},
		]
		messages = []
		with mock.patch.object(self.plugin, "_isChatGPTForeground", return_value=False), \
			mock.patch.object(backend, "_loadActiveTranscript", return_value=("task.jsonl", loaded)), \
			mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
			backend.firstMessage()
			backend.nextMessage()
			backend.readCurrentMessage()
			backend.lastMessage()

		self.assertEqual(messages, [
			"User message. Question",
			"Assistant message. Answer",
			"Assistant message. Answer",
			"Assistant message. Answer",
		])

	def test_send_chat_message_types_activates_send_and_schedules_reader(self):
		backend = self.plugin.ChatGPTBackend()
		composer = types.SimpleNamespace(value="", setFocus=mock.Mock())
		send_button = types.SimpleNamespace(doAction=mock.Mock())
		messages = []
		with mock.patch.object(self.plugin, "_chatgptForegroundRoot", return_value="root"):
			with mock.patch.object(self.plugin, "_findChatComposer", return_value=composer):
				with mock.patch.object(self.plugin, "_sendUnicodeText", return_value=True) as typeText:
					with mock.patch.object(self.plugin, "_findChatSendButton", return_value=send_button):
						with mock.patch.object(self.plugin.time, "sleep"):
							with mock.patch.object(self.plugin.wx, "CallLater") as callLater:
								with mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
									result = backend.sendChatMessage("Hello ChatGPT")

		self.assertTrue(result)
		composer.setFocus.assert_called_once_with()
		typeText.assert_called_once_with("Hello ChatGPT")
		send_button.doAction.assert_called_once_with()
		self.assertEqual(messages, ["Message sent. Waiting for ChatGPT"])
		callLater.assert_called_once_with(700, backend._pollChatResponse, 1, "Hello ChatGPT", None)

	def test_send_chat_message_refuses_work_or_codex_composer(self):
		backend = self.plugin.ChatGPTBackend()
		messages = []
		with mock.patch.object(self.plugin, "_chatgptForegroundRoot", return_value="root"):
			with mock.patch.object(self.plugin, "_findChatComposer", return_value=None):
				with mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
					result = backend.sendChatMessage("Do not send this")

		self.assertFalse(result)
		self.assertEqual(messages, ["Open a ChatGPT Chat or Quick chat first"])

	def test_load_codex_tasks_uses_state_database_and_excludes_archived_and_subagents(self):
		with tempfile.TemporaryDirectory() as temp_dir:
			database = os.path.join(temp_dir, "state_9.sqlite")
			connection = sqlite3.connect(database)
			connection.execute(
				"CREATE TABLE threads (id TEXT PRIMARY KEY, name TEXT, title TEXT, preview TEXT, "
				"first_user_message TEXT, cwd TEXT, archived INTEGER, thread_source TEXT, updated_at_ms INTEGER)"
			)
			connection.executemany(
				"INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
				[
					("task-new", "Real generated task name", "Opening words from the chat", "", "", r"C:\Work\Project One", 0, "user", 3000),
					("task-old", "", "", "Preview fallback", "", r"C:\Work\Project Two", 0, "user", 2000),
					("task-child", "Hidden child", "Child opening message", "", "", r"C:\Work\Project One", 0, "subagent", 4000),
					("task-archived", "Archived", "Archived opening message", "", "", r"C:\Work\Project One", 1, "user", 5000),
				],
			)
			connection.commit()
			connection.close()

			projects = [
				{"path": r"C:\Work\Project One", "name": "One"},
				{"path": r"C:\Work\Project Two", "name": "Two"},
			]
			with mock.patch.object(self.plugin, "_stateDatabasePaths", return_value=[database]):
				with mock.patch.object(self.plugin, "_taskProjectEntries", return_value=projects):
					tasks = self.plugin._loadCodexTasks()

		self.assertEqual([task["id"] for task in tasks], ["task-new", "task-old"])
		self.assertEqual([task["title"] for task in tasks], ["Real generated task name", "Preview fallback"])
		self.assertEqual([task["project"] for task in tasks], ["One", "Two"])

	def test_load_codex_tasks_falls_back_when_nvda_has_no_sqlite(self):
		records = [{"id": "fallback-task", "thread_name": "Fallback task", "updated_at": "2"}]
		with mock.patch.object(self.plugin, "sqlite3", None), \
			mock.patch.object(self.plugin, "_loadCodexTaskRowsWithSystemPython", return_value=None), \
			mock.patch.object(self.plugin, "_sessionIndexRecords", return_value=records), \
			mock.patch.object(self.plugin, "_sessionCwdById", return_value={"fallback-task": r"C:\Work\Fallback"}), \
			mock.patch.object(self.plugin, "_taskProjectEntries", return_value=[]):
			tasks = self.plugin._loadCodexTasks()
		self.assertEqual(tasks[0]["id"], "fallback-task")
		self.assertEqual(tasks[0]["title"], "Fallback task")
		self.assertEqual(tasks[0]["cwd"], r"C:\Work\Fallback")

	def test_load_codex_tasks_uses_system_python_when_nvda_has_no_sqlite(self):
		rows = [{
			"id": "database-task",
			"name": "Real database task name",
			"title": "Opening words from the chat",
			"preview": "",
			"firstUserMessage": "",
			"cwd": r"\\?\C:\Work\Voice Input",
			"updatedAt": 300,
		}]
		with mock.patch.object(self.plugin, "sqlite3", None), \
			mock.patch.object(self.plugin, "_stateDatabasePaths", return_value=["state.sqlite"]), \
			mock.patch.object(self.plugin, "_loadCodexTaskRowsWithSystemPython", return_value=rows) as helper, \
			mock.patch.object(self.plugin, "_sessionIndexRecords") as legacyIndex, \
			mock.patch.object(self.plugin, "_taskProjectEntries", return_value=[]):
			tasks = self.plugin._loadCodexTasks()

		helper.assert_called_once_with(["state.sqlite"])
		legacyIndex.assert_not_called()
		self.assertEqual([task["id"] for task in tasks], ["database-task"])
		self.assertEqual(tasks[0]["title"], "Real database task name")
		self.assertEqual(tasks[0]["cwd"], r"\\?\C:\Work\Voice Input")

	def test_chatgpt_database_helper_handles_unicode_with_windows_console_encoding(self):
		with tempfile.TemporaryDirectory() as temp_dir:
			database = os.path.join(temp_dir, "state.sqlite")
			connection = sqlite3.connect(database)
			connection.execute(
				"CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, preview TEXT, "
				"first_user_message TEXT, cwd TEXT, archived INTEGER, thread_source TEXT, updated_at_ms INTEGER)"
			)
			connection.execute(
				"INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
				("unicode-task", "Non\u2011breaking title", "", "", r"C:\Work", 0, "user", 100),
			)
			connection.commit()
			connection.close()

			process = subprocess.run(
				[sys.executable, str(DATABASE_HELPER_PATH), database],
				capture_output=True,
				timeout=10,
			)

		self.assertEqual(process.returncode, 0, process.stderr.decode(errors="replace"))
		payload = json.loads(process.stdout.decode("ascii"))
		self.assertEqual(payload["tasks"][0]["title"], "Non\u2011breaking title")

	def test_task_dialog_opens_selected_task_deep_link(self):
		dialog = types.SimpleNamespace(
			_selectedTask=lambda: {"id": "task id", "title": "Selected task"},
			_windowHandle=77,
			_codexPath=None,
			EndModal=mock.Mock(),
		)
		messages = []
		with mock.patch.object(self.plugin, "_openCodexUrlForSelection", return_value=True) as launch:
			with mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
				self.plugin._TaskDialog._onOpenTask(dialog, None)

		launch.assert_called_once_with("codex://threads/task%20id", 77, None)
		dialog.EndModal.assert_called_once_with(self.plugin.wx.ID_OK)
		self.assertEqual(messages, ["Opening Selected task"])

	def test_task_picker_opens_for_background_chatgpt_window(self):
		backend = self.plugin.ChatGPTBackend()
		with mock.patch.object(
			self.plugin,
			"_codexWindowDetails",
			return_value=(77, r"C:\Program Files\ChatGPT\ChatGPT.exe"),
		), mock.patch.object(self.plugin.wx, "CallAfter") as call_after, \
			mock.patch.object(self.plugin.ui, "message") as message:
			backend.openTaskPicker()

		call_after.assert_called_once_with(
			backend._showTaskPicker,
			77,
			r"C:\Program Files\ChatGPT\ChatGPT.exe",
		)
		message.assert_not_called()

	def test_task_picker_is_available_when_chatgpt_is_closed(self):
		backend = self.plugin.ChatGPTBackend()
		path = r"C:\Program Files\ChatGPT\ChatGPT.exe"
		with mock.patch.object(self.plugin, "_codexWindowDetails", return_value=(None, None)), \
			mock.patch.object(self.plugin, "_codexExecutablePath", return_value=path), \
			mock.patch.object(self.plugin.wx, "CallAfter") as call_after:
			backend.openTaskPicker()

		call_after.assert_called_once_with(backend._showTaskPicker, None, path)

	def test_cold_selection_launches_waits_then_opens_deep_link(self):
		class FakeThread:
			def __init__(self, target=None, **_kwargs):
				self._target = target

			def start(self):
				self._target()

		path = r"C:\Program Files\ChatGPT\ChatGPT.exe"
		url = "codex://threads/task-id"
		with mock.patch.object(self.plugin, "_codexExecutablePath", return_value=path), \
			mock.patch.object(self.plugin, "_launchCodex", return_value=True) as launch_app, \
			mock.patch.object(self.plugin, "_findCodexDesktopWindow", return_value=(77, path)), \
			mock.patch.object(self.plugin.time, "sleep"), \
			mock.patch.object(self.plugin.threading, "Thread", side_effect=lambda **kwargs: FakeThread(**kwargs)), \
			mock.patch.object(self.plugin.wx, "CallAfter", side_effect=lambda func, *args: func(*args)), \
			mock.patch.object(self.plugin, "_restoreCodexFocus") as restore_focus, \
			mock.patch.object(self.plugin, "_launchCodexUrl", return_value=True) as launch_url:
			result = self.plugin._openCodexUrlForSelection(url, None, path)

		self.assertTrue(result)
		launch_app.assert_called_once_with([], path)
		launch_url.assert_called_once_with(url)
		restore_focus.assert_called_once_with(77)

	def test_project_picker_opens_for_background_chatgpt_window(self):
		backend = self.plugin.ChatGPTBackend()
		path = r"C:\Program Files\ChatGPT\ChatGPT.exe"
		with mock.patch.object(self.plugin, "_codexWindowDetails", return_value=(88, path)), \
			mock.patch.object(self.plugin.wx, "CallAfter") as call_after, \
			mock.patch.object(self.plugin.ui, "message") as message:
			backend.openSessionPicker()

		call_after.assert_called_once_with(backend._showProjectPicker, 88, path)
		message.assert_not_called()

	def test_task_selection_relies_on_native_list_announcement(self):
		event = types.SimpleNamespace(Skip=mock.Mock())
		dialog = types.SimpleNamespace()
		with mock.patch.object(self.plugin.ui, "message") as message:
			self.plugin._TaskDialog._onTaskSelected(dialog, event)
		message.assert_not_called()
		event.Skip.assert_called_once_with()

	def test_project_task_selection_relies_on_native_list_announcement(self):
		event = types.SimpleNamespace(Skip=mock.Mock())
		dialog = types.SimpleNamespace()
		with mock.patch.object(self.plugin.ui, "message") as message:
			self.plugin._ProjectThreadDialog._onThreadSelected(dialog, event)
		message.assert_not_called()
		event.Skip.assert_called_once_with()

	def test_foreground_detection_rejects_browser_process(self):
		foreground = types.SimpleNamespace(
			appModule=types.SimpleNamespace(appPath=r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")
		)
		with mock.patch.object(self.plugin.api, "getForegroundObject", return_value=foreground):
			self.assertFalse(self.plugin._isChatGPTForeground())

	def test_desktop_executable_path_accepts_chatgpt_exe(self):
		chatgpt_path = r"C:\Program Files\WindowsApps\OpenAI.Codex_26.707.8479.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe"
		with mock.patch("builtins.open", mock.mock_open(read_data="")):
			with mock.patch.object(self.plugin.os.path, "isfile", side_effect=lambda path: path == chatgpt_path):
				with mock.patch.object(self.plugin.os.path, "isdir", return_value=False):
					self.assertEqual(self.plugin._codexExecutablePath(chatgpt_path), chatgpt_path)

	def test_restore_codex_focus_unminimizes_window(self):
		with mock.patch.object(self.plugin.ctypes.windll.user32, "IsIconic", return_value=1), \
			mock.patch.object(self.plugin.ctypes.windll.user32, "ShowWindow") as show_window, \
			mock.patch.object(self.plugin.winUser, "setForegroundWindow") as set_foreground:
			self.plugin._restoreCodexFocus(77)

		show_window.assert_called_once_with(77, 9)
		set_foreground.assert_called_once_with(77)

	def test_find_usage_windows_prefers_requested_durations(self):
		snapshot = {
			"primary": {"usedPercent": 66, "windowDurationMins": 300, "resetsAt": 1778121226},
			"secondary": {"usedPercent": 14, "windowDurationMins": 10080, "resetsAt": 1778611314},
		}

		windows = self.plugin._findUsageWindows(snapshot)

		self.assertEqual(windows["fiveHour"]["usedPercent"], 66)
		self.assertEqual(windows["weekly"]["usedPercent"], 14)

	def test_format_usage_summary_includes_partial_results(self):
		windows = {
			"fiveHour": {"usedPercent": 74, "windowDurationMins": 300, "resetsAt": 1778121226},
			"weekly": None,
		}
		with mock.patch.object(self.plugin, "_formatResetTime", return_value="9:20 PM"):
			message = self.plugin._formatUsageSummary(
				windows,
				None,
				{"availableCount": 2, "credits": [], "detailsAvailable": True},
			)

		self.assertIn("5-hour usage limit 26 percent remaining", message)
		self.assertIn("resets at 9:20 PM", message)
		self.assertIn("Weekly usage limit unavailable", message)
		self.assertIn("Banked usage resets: 2", message)

	def test_authentication_error_is_explained_for_nvda_users(self):
		message = self.plugin._friendlyUsageError(
			RuntimeError("codex account authentication required to read rate limits"),
			"fallback",
		)
		self.assertIn("Codex CLI is not signed in", message)
		self.assertIn("same ChatGPT account", message)

	def test_read_reset_credits_summary_keeps_authoritative_count_and_details(self):
		payload = {
			"rateLimitResetCredits": {
				"availableCount": 3,
				"credits": [
					{"id": "credit-1", "status": "available", "expiresAt": 200},
					{"id": "credit-2", "status": "redeemed", "expiresAt": 100},
				],
			}
		}

		summary = self.plugin._readResetCreditsSummary(payload)

		self.assertEqual(summary["availableCount"], 3)
		self.assertTrue(summary["detailsAvailable"])
		self.assertEqual(len(summary["credits"]), 2)

	def test_usage_state_keeps_banked_resets_when_usage_windows_are_missing(self):
		payload = {"rateLimitResetCredits": {"availableCount": 1}}
		with mock.patch.object(self.plugin, "_requestAppServerRateLimits", return_value=payload):
			state = self.plugin._readUsageState()
		self.assertIsNone(state["windows"]["fiveHour"])
		self.assertIsNone(state["windows"]["weekly"])
		self.assertEqual(state["resetCredits"]["availableCount"], 1)

	def test_preferred_reset_credit_uses_earliest_expiring_available_credit(self):
		summary = {
			"availableCount": 3,
			"credits": [
				{"id": "no-expiry", "status": "available", "expiresAt": None},
				{"id": "later", "status": "available", "expiresAt": 300},
				{"id": "redeemed", "status": "redeemed", "expiresAt": 100},
				{"id": "sooner", "status": "available", "expiresAt": 200},
			],
		}

		credit = self.plugin._preferredResetCredit(summary)

		self.assertEqual(credit["id"], "sooner")

	def test_consume_request_uses_idempotency_key_and_selected_credit(self):
		with mock.patch.object(self.plugin, "_runAppServerRequest", return_value={"outcome": "reset"}) as request:
			response = self.plugin._requestAppServerRateLimitReset("request-id", "credit-id")

		request.assert_called_once_with(
			"account/rateLimitResetCredit/consume",
			{"idempotencyKey": "request-id", "creditId": "credit-id"},
		)
		self.assertEqual(response["outcome"], "reset")

	def test_format_usage_summary_mentions_reached_limit(self):
		windows = {
			"fiveHour": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": None},
			"weekly": {"usedPercent": 14, "windowDurationMins": 10080, "resetsAt": None},
		}

		message = self.plugin._formatUsageSummary(windows, "rate_limit_reached")

		self.assertIn("5-hour limit reached", message)

	def test_format_reset_time_uses_full_date_for_weekly_window(self):
		text = self.plugin._formatResetTime(1778611314, self.plugin.WEEKLY_WINDOW_MINS)

		self.assertEqual(text, "Tuesday, May 12, 2026 12:41 PM")

	def test_read_rate_limits_prefers_bucket_map(self):
		response = {
			"rateLimits": {"primary": {"usedPercent": 1, "windowDurationMins": 300, "resetsAt": None}},
			"rateLimitsByLimitId": {
				"codex": {
					"primary": {"usedPercent": 66, "windowDurationMins": 300, "resetsAt": 1778121226},
					"secondary": {"usedPercent": 14, "windowDurationMins": 10080, "resetsAt": 1778611314},
					"rateLimitReachedType": None,
				}
			},
		}
		with mock.patch.object(self.plugin, "_requestAppServerRateLimits", return_value=response):
			snapshot = self.plugin._readRateLimitSnapshot()

		self.assertEqual(snapshot["primary"]["usedPercent"], 66)
		self.assertEqual(snapshot["secondary"]["usedPercent"], 14)

	def test_codex_cli_command_prefers_real_executable(self):
		with mock.patch.object(
			self.plugin.os.path,
			"isfile",
			side_effect=lambda path: path.endswith(os.path.join("bin", "codex.exe")),
		):
			command = self.plugin._codexCliCommand()

		self.assertTrue(command[0].endswith(os.path.join("bin", "codex.exe")))

	def test_app_server_launch_uses_hidden_window_flags(self):
		fake_proc = mock.Mock()
		fake_proc.stdout = iter([])
		fake_proc.stdin = mock.Mock()
		fake_proc.stderr = mock.Mock()
		with mock.patch.object(self.plugin.subprocess, "Popen", return_value=fake_proc) as popen:
			with mock.patch.object(self.plugin, "_awaitResponse", side_effect=RuntimeError("stop after launch")):
				with self.assertRaises(RuntimeError):
					self.plugin._runAppServerRequest("account/rateLimits/read")

		kwargs = popen.call_args.kwargs
		self.assertEqual(kwargs["creationflags"], self.plugin._subprocessCreationFlags())
		self.assertIsNotNone(kwargs["startupinfo"])

	def test_await_response_skips_notifications_before_matching_response(self):
		messages = queue.Queue()
		messages.put(json.dumps({"jsonrpc": "2.0", "method": "account/updated", "params": {}}))
		messages.put(json.dumps({"jsonrpc": "2.0", "id": "2", "result": {"ok": True}}))

		result = self.plugin._awaitResponse(messages, "2", 1.0)

		self.assertEqual(result, {"ok": True})

	def test_usage_script_does_not_require_foreground_focus(self):
		plugin_instance = self.plugin.GlobalPlugin()
		with mock.patch.object(plugin_instance, "_requireCodex", return_value=False):
			with mock.patch.object(plugin_instance, "_startUsageWorker") as start_worker:
				plugin_instance.script_reportCodexUsageLimits(None)

		start_worker.assert_called_once()

	def test_usage_command_routes_second_press_to_reset_confirmation(self):
		plugin_instance = self.plugin.GlobalPlugin()
		with mock.patch.object(plugin_instance, "reportUsageLimits") as report:
			with mock.patch.object(plugin_instance, "promptUsageReset") as prompt:
				plugin_instance.handleUsageCommand(0)
				plugin_instance.handleUsageCommand(1)
				plugin_instance.handleUsageCommand(2)

		report.assert_called_once_with()
		prompt.assert_called_once_with()

	def test_reset_confirmation_defaults_to_no_and_does_not_consume(self):
		plugin_instance = self.plugin.GlobalPlugin()
		state = {
			"windows": {"fiveHour": {}, "weekly": {}},
			"resetCredits": {
				"availableCount": 1,
				"credits": [{"id": "credit-1", "status": "available", "expiresAt": None}],
			},
		}
		with mock.patch.object(self.plugin.gui, "messageBox", return_value=self.plugin.wx.NO) as dialog:
			with mock.patch.object(plugin_instance, "_consumeUsageReset") as consume:
				plugin_instance._finishUsageResetCheck(plugin_instance._usageRequestSerial, state, None)

		dialog.assert_called_once()
		consume.assert_not_called()

	def test_reset_confirmation_consumes_only_after_yes(self):
		plugin_instance = self.plugin.GlobalPlugin()
		state = {
			"windows": {"fiveHour": {}, "weekly": {}},
			"resetCredits": {
				"availableCount": 2,
				"credits": [{"id": "credit-1", "status": "available", "expiresAt": None}],
			},
		}
		with mock.patch.object(self.plugin.gui, "messageBox", return_value=self.plugin.wx.YES):
			with mock.patch.object(plugin_instance, "_consumeUsageReset") as consume:
				plugin_instance._finishUsageResetCheck(plugin_instance._usageRequestSerial, state, None)

		consume.assert_called_once_with("credit-1")

	def test_new_chat_uses_codex_url_for_selected_project(self):
		messages = []
		dialog = types.SimpleNamespace(
			_selectedProject=lambda: {"path": r"C:\Work\Project One", "name": "Project One"},
			_windowHandle=123,
			_codexPath=None,
			EndModal=mock.Mock(),
		)
		with mock.patch.object(self.plugin, "_setActiveProjectRoot", return_value=True) as setRoot:
			with mock.patch.object(self.plugin, "_openCodexUrlForSelection", return_value=True) as launchUrl:
				with mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
					self.plugin._ProjectThreadDialog._onNewChat(dialog, None)

		setRoot.assert_called_once_with(r"C:\Work\Project One")
		launchUrl.assert_called_once_with("codex://new?path=C%3A%5CWork%5CProject%20One", 123, None)
		dialog.EndModal.assert_called_once_with(self.plugin.wx.ID_OK)
		self.assertEqual(messages, ["Starting a new task in Project One"])

	def test_open_project_folder_opens_selected_project_in_explorer(self):
		messages = []
		dialog = types.SimpleNamespace(
			_selectedProject=lambda: {"path": r"C:\Work\Project One", "name": "Project One"},
			EndModal=mock.Mock(),
		)
		with mock.patch.object(self.plugin, "_openFolderInExplorer", return_value=True) as openFolder:
			with mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
				self.plugin._ProjectThreadDialog._onOpenProjectFolder(dialog, None)

		openFolder.assert_called_once_with(r"C:\Work\Project One")
		dialog.EndModal.assert_not_called()
		self.assertEqual(messages, ["Opening folder for Project One"])

	def test_open_project_folder_requires_selection(self):
		messages = []
		dialog = types.SimpleNamespace(
			_selectedProject=lambda: None,
			EndModal=mock.Mock(),
		)
		with mock.patch.object(self.plugin, "_openFolderInExplorer") as openFolder:
			with mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
				self.plugin._ProjectThreadDialog._onOpenProjectFolder(dialog, None)

		openFolder.assert_not_called()
		dialog.EndModal.assert_not_called()
		self.assertEqual(messages, ["Choose a project first"])

	def test_remove_project_root_removes_saved_entries_and_labels(self):
		state = {
			"project-order": [r"C:\Work\Project One", r"C:\Work\Project Two"],
			"active-workspace-roots": [r"C:\Work\Project One"],
			"electron-saved-workspace-roots": [r"C:\Work\Project One", r"C:\Work\Project Two"],
			"electron-workspace-root-labels": {
				r"C:\Work\Project One": "Project One Custom",
				r"C:\Work\Project Two": "Project Two Custom",
			},
			"electron-persisted-atom-state": {
				"project-order": [r"C:\Work\Project One", r"C:\Work\Project Two"],
				"active-workspace-roots": [r"C:\Work\Project One"],
				"electron-workspace-root-labels": {
					r"C:\Work\Project One": "Persisted One",
				},
			},
		}
		picker_state = {
			"project-labels": {
				r"C:\Work\Project One": "Picker One",
			},
		}
		written = {}

		def read_json(path):
			if path == self.plugin.GLOBAL_STATE_PATH:
				return state
			if path == self.plugin.PROJECT_PICKER_STATE_PATH:
				return picker_state
			return None

		def capture_write(path, payload):
			written[path] = payload

		with mock.patch.object(self.plugin, "_readJsonFile", side_effect=read_json):
			with mock.patch.object(self.plugin, "_writeJsonFile", side_effect=capture_write):
				result = self.plugin._removeProjectRoot(r"C:\Work\Project One")

		self.assertTrue(result)
		payload = written[self.plugin.GLOBAL_STATE_PATH]
		self.assertEqual(payload["project-order"], [r"C:\Work\Project Two"])
		self.assertEqual(payload["active-workspace-roots"], [r"C:\Work\Project Two"])
		self.assertEqual(payload["workspace-root-options"], [r"C:\Work\Project Two"])
		self.assertEqual(payload["electron-saved-workspace-roots"], [r"C:\Work\Project Two"])
		self.assertEqual(payload["electron-workspace-root-labels"], {r"C:\Work\Project Two": "Project Two Custom"})
		self.assertEqual(payload["electron-persisted-atom-state"]["project-order"], [r"C:\Work\Project Two"])
		self.assertEqual(payload["electron-persisted-atom-state"]["active-workspace-roots"], [r"C:\Work\Project Two"])
		self.assertEqual(payload["electron-persisted-atom-state"]["electron-saved-workspace-roots"], [r"C:\Work\Project Two"])
		self.assertEqual(payload["electron-persisted-atom-state"]["electron-workspace-root-labels"], {})
		picker_payload = written[self.plugin.PROJECT_PICKER_STATE_PATH]
		self.assertEqual(picker_payload["hidden-project-roots"], [r"C:\Work\Project One"])
		self.assertEqual(picker_payload["project-labels"], {})

	def test_rename_project_root_sets_label_override(self):
		state = {
			"project-order": [r"C:\Work\Project One"],
			"electron-persisted-atom-state": {},
		}
		picker_state = {}
		written = {}

		def read_json(path):
			if path == self.plugin.GLOBAL_STATE_PATH:
				return state
			if path == self.plugin.PROJECT_PICKER_STATE_PATH:
				return picker_state
			return None

		def capture_write(path, payload):
			written[path] = payload

		with mock.patch.object(self.plugin, "_readJsonFile", side_effect=read_json):
			with mock.patch.object(self.plugin, "_writeJsonFile", side_effect=capture_write):
				result = self.plugin._renameProjectRoot(r"C:\Work\Project One", "Renamed Project")

		self.assertTrue(result)
		payload = written[self.plugin.GLOBAL_STATE_PATH]
		self.assertEqual(payload["electron-workspace-root-labels"], {r"C:\Work\Project One": "Renamed Project"})
		self.assertEqual(payload["electron-persisted-atom-state"]["electron-workspace-root-labels"], {r"C:\Work\Project One": "Renamed Project"})
		picker_payload = written[self.plugin.PROJECT_PICKER_STATE_PATH]
		self.assertEqual(picker_payload["project-labels"], {})
		self.assertEqual(picker_payload["hidden-project-roots"], [])

	def test_set_active_project_root_unhides_root_and_promotes_saved_root(self):
		state = {
			"project-order": [r"C:\Work\Project Two"],
			"active-workspace-roots": [r"C:\Work\Project Two"],
			"electron-saved-workspace-roots": [r"C:\Work\Project Two"],
			"electron-persisted-atom-state": {
				"project-order": [r"C:\Work\Project Two"],
				"active-workspace-roots": [r"C:\Work\Project Two"],
			},
		}
		picker_state = {
			"hidden-project-roots": [r"C:\Work\Project One"],
		}
		written = {}

		def read_json(path):
			if path == self.plugin.GLOBAL_STATE_PATH:
				return state
			if path == self.plugin.PROJECT_PICKER_STATE_PATH:
				return picker_state
			return None

		def capture_write(path, payload):
			written[path] = payload

		with mock.patch.object(self.plugin, "_readJsonFile", side_effect=read_json):
			with mock.patch.object(self.plugin, "_writeJsonFile", side_effect=capture_write):
				result = self.plugin._setActiveProjectRoot(r"C:\Work\Project One")

		self.assertTrue(result)
		payload = written[self.plugin.GLOBAL_STATE_PATH]
		self.assertEqual(payload["project-order"], [r"C:\Work\Project One", r"C:\Work\Project Two"])
		self.assertEqual(payload["active-workspace-roots"], [r"C:\Work\Project One", r"C:\Work\Project Two"])
		self.assertEqual(payload["electron-saved-workspace-roots"], [r"C:\Work\Project One", r"C:\Work\Project Two"])
		picker_payload = written[self.plugin.PROJECT_PICKER_STATE_PATH]
		self.assertEqual(picker_payload["hidden-project-roots"], [])

	def test_load_codex_projects_prefers_saved_label_override(self):
		state = {
			"project-order": [r"C:\Work\Project One"],
		}
		picker_state = {
			"project-labels": {
				r"C:\Work\Project One": "Renamed Project",
			},
		}

		def read_json(path):
			if path == self.plugin.GLOBAL_STATE_PATH:
				return state
			if path == self.plugin.PROJECT_PICKER_STATE_PATH:
				return picker_state
			return None

		with mock.patch.object(self.plugin, "_readJsonFile", side_effect=read_json):
			with mock.patch.object(self.plugin, "_loadCodexTasks", return_value=[]):
				projects = self.plugin._loadCodexProjects()["projects"]

		self.assertEqual(projects[0]["name"], "Renamed Project")

	def test_load_codex_projects_hides_removed_root_even_when_session_history_exists(self):
		state = {}
		picker_state = {
			"hidden-project-roots": [r"C:\Work\Project One"],
		}

		def read_json(path):
			if path == self.plugin.GLOBAL_STATE_PATH:
				return state
			if path == self.plugin.PROJECT_PICKER_STATE_PATH:
				return picker_state
			return None

		tasks = [{
			"id": "session-one",
			"title": "Hidden thread",
			"cwd": r"C:\Work\Project One",
			"updatedAt": 100,
		}]
		with mock.patch.object(self.plugin, "_readJsonFile", side_effect=read_json):
			with mock.patch.object(self.plugin, "_loadCodexTasks", return_value=tasks):
				projects = self.plugin._loadCodexProjects()["projects"]

		self.assertEqual(projects, [])

	def test_load_codex_projects_keeps_explicit_saved_root_visible_even_when_picker_state_hides_it(self):
		state = {
			"project-order": [r"C:\Work\Project One"],
		}
		picker_state = {
			"hidden-project-roots": [r"C:\Work\Project One"],
		}

		def read_json(path):
			if path == self.plugin.GLOBAL_STATE_PATH:
				return state
			if path == self.plugin.PROJECT_PICKER_STATE_PATH:
				return picker_state
			return None

		with mock.patch.object(self.plugin, "_readJsonFile", side_effect=read_json):
			with mock.patch.object(self.plugin, "_loadCodexTasks", return_value=[]):
				projects = self.plugin._loadCodexProjects()["projects"]

		self.assertEqual([project["path"] for project in projects], [r"C:\Work\Project One"])

	def test_remove_project_handler_refreshes_picker(self):
		messages = []
		dialog = types.SimpleNamespace(
			_selectedProject=lambda: {"path": r"C:\Work\Project One", "name": "Project One"},
			_refreshProjects=mock.Mock(),
			Hide=mock.Mock(),
			Show=mock.Mock(),
			Raise=mock.Mock(),
			EndModal=mock.Mock(),
			_windowHandle=123,
		)
		with mock.patch.object(self.plugin, "_removeProjectFromCodex", return_value=True) as removeFromCodex:
			with mock.patch.object(self.plugin, "_restartCodexWithStateMutation", return_value=True) as restartMutation:
				with mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
					self.plugin._ProjectThreadDialog._onRemoveProject(dialog, None)

		removeFromCodex.assert_called_once_with(123, "Project One", r"C:\Work\Project One")
		dialog.Hide.assert_called_once_with()
		dialog.Show.assert_not_called()
		dialog.Raise.assert_not_called()
		dialog._refreshProjects.assert_called_once_with()
		dialog.EndModal.assert_called_once_with(self.plugin.wx.ID_OK)
		restartMutation.assert_not_called()
		self.assertEqual(messages, ["Project One removed"])

	def test_rename_project_handler_refreshes_picker(self):
		messages = []
		fake_text_dialog = mock.Mock()
		fake_text_dialog.ShowModal.return_value = self.plugin.wx.ID_OK
		fake_text_dialog.GetValue.return_value = "Renamed Project"
		dialog = types.SimpleNamespace(
			_selectedProject=lambda: {"path": r"C:\Work\Project One", "name": "Project One"},
			_refreshProjects=mock.Mock(),
			Hide=mock.Mock(),
			Show=mock.Mock(),
			Raise=mock.Mock(),
			EndModal=mock.Mock(),
			_windowHandle=456,
		)
		with mock.patch.object(self.plugin.wx, "TextEntryDialog", return_value=fake_text_dialog):
			with mock.patch.object(self.plugin, "_renameProjectInCodex", return_value=True) as renameInCodex:
				with mock.patch.object(self.plugin, "_restartCodexWithStateMutation", return_value=True) as restartMutation:
					with mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
						self.plugin._ProjectThreadDialog._onRenameProject(dialog, None)

		renameInCodex.assert_called_once_with(456, "Project One", r"C:\Work\Project One", "Renamed Project")
		dialog.Hide.assert_called_once_with()
		dialog.Show.assert_not_called()
		dialog.Raise.assert_not_called()
		dialog._refreshProjects.assert_called_once_with(preferredPath=r"C:\Work\Project One")
		dialog.EndModal.assert_called_once_with(self.plugin.wx.ID_OK)
		restartMutation.assert_not_called()
		self.assertEqual(messages, ["Project One renamed to Renamed Project"])
		fake_text_dialog.Destroy.assert_called_once_with()

	def test_remove_project_handler_restores_dialog_when_codex_action_fails(self):
		messages = []
		dialog = types.SimpleNamespace(
			_selectedProject=lambda: {"path": r"C:\Work\Project One", "name": "Project One"},
			_refreshProjects=mock.Mock(),
			Hide=mock.Mock(),
			Show=mock.Mock(),
			Raise=mock.Mock(),
			EndModal=mock.Mock(),
			_codexPath=r"C:\Program Files\Codex\codex.exe",
			_windowHandle=123,
		)
		with mock.patch.object(self.plugin, "_removeProjectFromCodex", return_value=False) as removeFromCodex:
			with mock.patch.object(
				self.plugin,
				"_restartCodexWithStateMutationAsync",
				side_effect=lambda _handle, _mutate, _path, onComplete=None: (onComplete(True) if callable(onComplete) else None) or True,
			) as restartMutation:
				with mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
					self.plugin._ProjectThreadDialog._onRemoveProject(dialog, None)

		removeFromCodex.assert_called_once_with(123, "Project One", r"C:\Work\Project One")
		restartMutation.assert_called_once()
		dialog.Hide.assert_called_once_with()
		dialog.Show.assert_not_called()
		dialog.Raise.assert_not_called()
		dialog.EndModal.assert_called_once_with(self.plugin.wx.ID_OK)
		dialog._refreshProjects.assert_called_once_with()
		self.assertEqual(messages, ["Project One removed"])

	def test_rename_project_handler_restores_dialog_when_codex_action_fails(self):
		messages = []
		fake_text_dialog = mock.Mock()
		fake_text_dialog.ShowModal.return_value = self.plugin.wx.ID_OK
		fake_text_dialog.GetValue.return_value = "Renamed Project"
		dialog = types.SimpleNamespace(
			_selectedProject=lambda: {"path": r"C:\Work\Project One", "name": "Project One"},
			_refreshProjects=mock.Mock(),
			Hide=mock.Mock(),
			Show=mock.Mock(),
			Raise=mock.Mock(),
			EndModal=mock.Mock(),
			_codexPath=r"C:\Program Files\Codex\codex.exe",
			_windowHandle=456,
		)
		with mock.patch.object(self.plugin.wx, "TextEntryDialog", return_value=fake_text_dialog):
			with mock.patch.object(self.plugin, "_renameProjectInCodex", return_value=False) as renameInCodex:
				with mock.patch.object(
					self.plugin,
					"_restartCodexWithStateMutationAsync",
					side_effect=lambda _handle, _mutate, _path, onComplete=None: (onComplete(True) if callable(onComplete) else None) or True,
				) as restartMutation:
					with mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
						self.plugin._ProjectThreadDialog._onRenameProject(dialog, None)

		renameInCodex.assert_called_once_with(456, "Project One", r"C:\Work\Project One", "Renamed Project")
		restartMutation.assert_called_once()
		dialog.Hide.assert_called_once_with()
		dialog.Show.assert_not_called()
		dialog.Raise.assert_not_called()
		dialog.EndModal.assert_called_once_with(self.plugin.wx.ID_OK)
		dialog._refreshProjects.assert_called_once_with(preferredPath=r"C:\Work\Project One")
		self.assertEqual(messages, ["Project One renamed to Renamed Project"])
		fake_text_dialog.Destroy.assert_called_once_with()

	def test_remove_project_handler_restores_dialog_when_async_restart_fails(self):
		messages = []
		dialog = types.SimpleNamespace(
			_selectedProject=lambda: {"path": r"C:\Work\Project One", "name": "Project One"},
			_refreshProjects=mock.Mock(),
			Hide=mock.Mock(),
			Show=mock.Mock(),
			Raise=mock.Mock(),
			EndModal=mock.Mock(),
			_codexPath=r"C:\Program Files\Codex\codex.exe",
			_windowHandle=123,
		)
		with mock.patch.object(self.plugin, "_removeProjectFromCodex", return_value=False):
			with mock.patch.object(
				self.plugin,
				"_restartCodexWithStateMutationAsync",
				side_effect=lambda _handle, _mutate, _path, onComplete=None: (onComplete(False) if callable(onComplete) else None) or True,
			):
				with mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
					self.plugin._ProjectThreadDialog._onRemoveProject(dialog, None)

		dialog.Hide.assert_called_once_with()
		dialog.Show.assert_called_once_with()
		dialog.Raise.assert_called_once_with()
		dialog.EndModal.assert_not_called()
		dialog._refreshProjects.assert_not_called()
		self.assertEqual(messages, ["Could not remove the selected project from ChatGPT"])

	def test_rename_project_handler_restores_dialog_when_async_restart_fails(self):
		messages = []
		fake_text_dialog = mock.Mock()
		fake_text_dialog.ShowModal.return_value = self.plugin.wx.ID_OK
		fake_text_dialog.GetValue.return_value = "Renamed Project"
		dialog = types.SimpleNamespace(
			_selectedProject=lambda: {"path": r"C:\Work\Project One", "name": "Project One"},
			_refreshProjects=mock.Mock(),
			Hide=mock.Mock(),
			Show=mock.Mock(),
			Raise=mock.Mock(),
			EndModal=mock.Mock(),
			_codexPath=r"C:\Program Files\Codex\codex.exe",
			_windowHandle=456,
		)
		with mock.patch.object(self.plugin.wx, "TextEntryDialog", return_value=fake_text_dialog):
			with mock.patch.object(self.plugin, "_renameProjectInCodex", return_value=False):
				with mock.patch.object(
					self.plugin,
					"_restartCodexWithStateMutationAsync",
					side_effect=lambda _handle, _mutate, _path, onComplete=None: (onComplete(False) if callable(onComplete) else None) or True,
				):
					with mock.patch.object(self.plugin.ui, "message", side_effect=messages.append):
						self.plugin._ProjectThreadDialog._onRenameProject(dialog, None)

		dialog.Hide.assert_called_once_with()
		dialog.Show.assert_called_once_with()
		dialog.Raise.assert_called_once_with()
		dialog.EndModal.assert_not_called()
		dialog._refreshProjects.assert_not_called()
		self.assertEqual(messages, ["Could not rename the selected project in ChatGPT"])
		fake_text_dialog.Destroy.assert_called_once_with()

	def test_restart_codex_with_state_mutation_async_completes_in_background(self):
		callback_results = []

		class FakeThread:
			def __init__(self, target=None, name=None, daemon=None):
				self._target = target

			def start(self):
				self._target()

		with mock.patch.object(self.plugin, "_codexExecutablePath", return_value=r"C:\Program Files\Codex\codex.exe"):
			with mock.patch.object(self.plugin, "_waitForWindowClosed", return_value=True):
				with mock.patch.object(self.plugin.time, "sleep"):
					with mock.patch.object(self.plugin, "_launchCodex", return_value=True):
						with mock.patch.object(self.plugin.wx, "CallAfter", side_effect=lambda func, *args: func(*args)):
							with mock.patch.object(self.plugin.threading, "Thread", side_effect=lambda **kwargs: FakeThread(**kwargs)) as threadFactory:
								with mock.patch.object(self.plugin.ctypes.windll.user32, "PostMessageW", return_value=1):
									mutate = mock.Mock(return_value=True)
									result = self.plugin._restartCodexWithStateMutationAsync(
										77,
										mutate,
										r"C:\Program Files\Codex\codex.exe",
										onComplete=callback_results.append,
									)

		self.assertTrue(result)
		threadFactory.assert_called_once()
		mutate.assert_called_once_with()
		self.assertEqual(callback_results, [True])

	def test_wait_for_window_closed_returns_true_when_window_disappears(self):
		with mock.patch.object(self.plugin.ctypes.windll.user32, "IsWindow", side_effect=[1, 1, 0]):
			with mock.patch.object(self.plugin.time, "sleep"):
				result = self.plugin._waitForWindowClosed(77, 1.0)

		self.assertTrue(result)

	def test_restart_codex_with_state_mutation_closes_mutates_and_relaunches(self):
		with mock.patch.object(self.plugin, "_codexExecutablePath", return_value=r"C:\Program Files\Codex\codex.exe"):
			with mock.patch.object(self.plugin, "_windowProcessId", return_value=321) as windowProcessId:
				with mock.patch.object(self.plugin, "_waitForProcessExit", return_value=True) as waitForExit:
					with mock.patch.object(self.plugin, "_launchCodex", return_value=True) as launchCodex:
						with mock.patch.object(self.plugin.ctypes.windll.user32, "PostMessageW", return_value=1) as postMessage:
							mutate = mock.Mock(return_value=True)
							result = self.plugin._restartCodexWithStateMutation(77, mutate, r"C:\Program Files\Codex\codex.exe")

		self.assertTrue(result)
		windowProcessId.assert_called_once_with(77)
		postMessage.assert_called_once_with(77, 0x0010, 0, 0)
		waitForExit.assert_called_once_with(321, 5.0)
		mutate.assert_called_once_with()
		launchCodex.assert_called_once_with([], r"C:\Program Files\Codex\codex.exe")

	def test_restart_codex_posts_close_and_launches_again(self):
		call_later = mock.Mock()
		with mock.patch.object(self.plugin, "_codexExecutablePath", return_value=r"C:\Program Files\Codex\codex.exe"):
			with mock.patch.object(self.plugin.wx, "CallLater", call_later):
				with mock.patch.object(self.plugin.ctypes.windll.user32, "PostMessageW", return_value=1) as postMessage:
					result = self.plugin._restartCodex(77, r"C:\Program Files\Codex\codex.exe")

		self.assertTrue(result)
		postMessage.assert_called_once_with(77, 0x0010, 0, 0)
		call_later.assert_called_once_with(1200, self.plugin._launchCodex, [], r"C:\Program Files\Codex\codex.exe")

	def test_activate_codex_project_menu_item_clicks_requested_action(self):
		menu_control = object()
		menu_item = mock.Mock()
		with mock.patch.object(self.plugin, "_restoreCodexFocus") as restoreFocus:
			with mock.patch.object(self.plugin, "_codexRootWebArea", return_value=("window", "root")):
				with mock.patch.object(self.plugin, "_codexProjectActionButton", return_value="button") as actionButton:
					with mock.patch.object(self.plugin, "_openCodexProjectActions", return_value=menu_control) as openMenu:
						with mock.patch.object(self.plugin, "_visibleUiAutomationControl", side_effect=[menu_item]):
							result = self.plugin._activateCodexProjectMenuItem(
								"Project One",
								"Rename project",
								projectPath=r"C:\Work\Project One",
								windowHandle=99,
							)

		self.assertTrue(result)
		restoreFocus.assert_called_once_with(99)
		actionButton.assert_called_once_with("root", "Project One", projectPath=r"C:\Work\Project One")
		openMenu.assert_called_once_with("window", "button")
		menu_item.Click.assert_called_once_with()

	def test_rename_project_in_codex_sets_new_label_and_saves(self):
		edit = mock.Mock()
		value_pattern = mock.Mock()
		edit.GetValuePattern.return_value = value_pattern
		save_button = mock.Mock()
		with mock.patch.object(self.plugin, "_activateCodexProjectMenuItem", return_value=True) as activateMenuItem:
			with mock.patch.object(self.plugin, "_codexRootWebArea", return_value=("window", None)):
				with mock.patch.object(self.plugin, "_visibleUiAutomationControl", side_effect=[edit, save_button]):
					result = self.plugin._renameProjectInCodex(42, "Project One", r"C:\Work\Project One", "Renamed Project")

		self.assertTrue(result)
		activateMenuItem.assert_called_once_with(
			"Project One",
			"Rename project",
			projectPath=r"C:\Work\Project One",
			windowHandle=42,
		)
		value_pattern.SetValue.assert_called_once_with("Renamed Project")
		save_button.Click.assert_called_once_with()

	def test_remove_project_from_codex_confirms_remove(self):
		confirm_button = mock.Mock()
		with mock.patch.object(self.plugin, "_activateCodexProjectMenuItem", return_value=True) as activateMenuItem:
			with mock.patch.object(self.plugin, "_codexRootWebArea", return_value=("window", None)):
				with mock.patch.object(self.plugin, "_visibleUiAutomationControl", return_value=confirm_button):
					result = self.plugin._removeProjectFromCodex(7, "Project One", r"C:\Work\Project One")

		self.assertTrue(result)
		activateMenuItem.assert_called_once_with(
			"Project One",
			"Remove",
			projectPath=r"C:\Work\Project One",
			windowHandle=7,
		)
		confirm_button.Click.assert_called_once_with()

	def test_load_codex_projects_merges_saved_workspace_roots_even_when_project_order_exists(self):
		state = {
			"project-order": [r"C:\Work\Older Project"],
			"electron-saved-workspace-roots": [
				r"C:\Work\Older Project",
				r"C:\Work\New Project",
			],
			"active-workspace-roots": [r"C:\Work\Older Project"],
		}
		with mock.patch.object(self.plugin, "_readJsonFile", return_value=state):
			with mock.patch.object(self.plugin, "_loadCodexTasks", return_value=[]):
				projects = self.plugin._loadCodexProjects()["projects"]

		self.assertEqual(
			[project["path"] for project in projects],
			[r"C:\Work\Older Project", r"C:\Work\New Project"],
		)

	def test_load_codex_projects_includes_session_roots_missing_from_saved_state(self):
		state = {
			"project-order": [r"C:\Work\Older Project"],
			"active-workspace-roots": [r"C:\Work\Older Project"],
		}
		tasks = [
			{"id": "session-new", "title": "New thread", "cwd": r"C:\Work\Brand New Project", "updatedAt": 200},
			{"id": "session-old", "title": "Old thread", "cwd": r"C:\Work\Older Project", "updatedAt": 100},
		]
		with mock.patch.object(self.plugin, "_readJsonFile", return_value=state):
			with mock.patch.object(self.plugin, "_loadCodexTasks", return_value=tasks):
				projects = self.plugin._loadCodexProjects()["projects"]

		self.assertEqual(
			[project["path"] for project in projects],
			[r"C:\Work\Older Project", r"C:\Work\Brand New Project"],
		)
		self.assertEqual(projects[1]["threads"][0]["title"], "New thread")

	def test_load_codex_projects_includes_database_task_missing_from_legacy_index(self):
		state = {
			"electron-saved-workspace-roots": [r"C:\Work\NVDA Voice Input"],
		}
		tasks = [{
			"id": "database-only-task",
			"title": "Voice input task",
			# The current state database stores Windows extended-length paths,
			# while ChatGPT's saved project roots use ordinary drive paths.
			"cwd": r"\\?\C:\Work\NVDA Voice Input",
			"updatedAt": 300,
		}]
		with mock.patch.object(self.plugin, "_readJsonFile", return_value=state):
			with mock.patch.object(self.plugin, "_loadCodexTasks", return_value=tasks):
				with mock.patch.object(self.plugin, "_sessionIndexRecords", return_value=[]) as legacyIndex:
					projects = self.plugin._loadCodexProjects()["projects"]

		self.assertEqual(len(projects), 1)
		self.assertEqual([thread["id"] for thread in projects[0]["threads"]], ["database-only-task"])
		legacyIndex.assert_not_called()


if __name__ == "__main__":
	unittest.main()
