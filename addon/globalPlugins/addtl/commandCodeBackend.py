# -*- coding: UTF-8 -*-
# globalPlugins/addtl/commandCodeBackend.py
#
# Command Code backend for agentDesktopAccessibility.
#
# Command Code Desktop is an Electron app that runs the command-code CLI
# underneath. Two stores matter to the add-on, and both are plain JSON, so
# NVDA's embedded Python can read them without the sqlite3 dance the OpenCode
# and Freebuff backends need:
#
#   %APPDATA%\Command Code\app-state.json
#       Renderer state: appState.activeThreadId plus any cached messages.
#
#   ~/.commandcode/projects/<project-slug>/<thread-id>.jsonl
#       The CLI transcript, one JSON object per line: a "session" header,
#       then "message" records whose message.content is a list of text and
#       thinking parts. This is the full conversation.
#
# The desktop app does not register a deep link for opening a thread (its
# only commandcode:// route is the OAuth callback), so thread switching goes
# through the accessibility tree — see TranscriptBackend._activateByTitle.

import glob
import json
import os

from logHandler import log

from .transcriptBackend import TranscriptBackend

_APP_STATE_CANDIDATES = [
	os.path.join(os.environ.get("APPDATA", ""), "Command Code", "app-state.json"),
]

# ~/.commandcode/projects/<slug>/<thread-id>.jsonl
_TRANSCRIPT_ROOTS = [
	os.path.join(os.path.expanduser("~"), ".commandcode", "projects"),
]

# Title extraction reads at most this much of a transcript before giving up.
_TITLE_SCAN_BYTES = 65536


def _firstExisting(paths):
	for path in paths:
		if path and os.path.isfile(path):
			return path
	return None


def _folderLabel(path):
	"""Last path component, falling back to the whole path for drive roots."""
	trimmed = (path or "").rstrip("/\\")
	if not trimmed:
		return ""
	return os.path.basename(trimmed) or trimmed


class CommandCodeBackend(TranscriptBackend):

	appLabel = "Command Code"
	threadNoun = "thread"
	logPrefix = "commandCodeAccessibility"

	def __init__(self, plugin=None):
		super().__init__(plugin)
		self._state = None
		self._stateKey = None
		log.info("Command Code backend loaded")

	# ------------------------------------------------------------------
	# app-state.json
	# ------------------------------------------------------------------

	def _appState(self):
		path = _firstExisting(_APP_STATE_CANDIDATES)
		if not path:
			return {}
		try:
			stat = os.stat(path)
			key = (path, stat.st_mtime, stat.st_size)
		except OSError:
			return {}
		if self._state is not None and key == self._stateKey:
			return self._state
		try:
			with open(path, encoding="utf-8") as handle:
				data = json.load(handle)
			self._state = data.get("appState") or {}
			self._stateKey = key
		except Exception as e:
			log.warning("Command Code: could not read app-state.json: %s", e)
			self._dbg("app-state error:", e)
			if self._state is None:
				self._state = {}
		return self._state

	def _stateThreads(self):
		"""id -> thread record, from the renderer's cached thread list."""
		threads = {}
		for thread in self._appState().get("threads") or []:
			thread_id = thread.get("id")
			if thread_id:
				threads[thread_id] = thread
		return threads

	# ------------------------------------------------------------------
	# Transcript files
	# ------------------------------------------------------------------

	def _transcriptPaths(self):
		"""(threadId, path) for every transcript on disk with content."""
		found = []
		for root in _TRANSCRIPT_ROOTS:
			if not os.path.isdir(root):
				continue
			for path in glob.glob(os.path.join(root, "*", "*.jsonl")):
				name = os.path.basename(path)
				if name.endswith(".checkpoints.jsonl"):
					continue
				try:
					# The CLI leaves zero-byte transcripts behind for scratch
					# sessions; they have no title and nothing to read.
					if os.path.getsize(path) == 0:
						continue
				except OSError:
					continue
				found.append((name[:-len(".jsonl")], path))
		return found

	def _transcriptPath(self, thread_id):
		for found_id, path in self._transcriptPaths():
			if found_id == thread_id:
				return path
		return None

	def _parseTranscript(self, path):
		messages = []
		try:
			with open(path, encoding="utf-8", errors="replace") as handle:
				for line in handle:
					line = line.strip()
					if not line:
						continue
					try:
						record = json.loads(line)
					except ValueError:
						continue
					if record.get("type") != "message":
						continue
					message = record.get("message") or {}
					role_raw = (message.get("role") or "").lower()
					if role_raw not in ("user", "assistant"):
						continue
					texts, thoughts = [], []
					content = message.get("content")
					if isinstance(content, list):
						for part in content:
							if not isinstance(part, dict):
								continue
							kind = part.get("type")
							if kind == "text":
								value = part.get("text") or ""
							elif kind in ("thinking", "reasoning"):
								value = part.get("thinking") or part.get("text") or ""
								if value.strip():
									thoughts.append(value.strip())
								continue
							else:
								continue
							if value.strip():
								texts.append(value.strip())
					elif isinstance(content, str) and content.strip():
						texts.append(content.strip())
					text = "\n".join(texts).strip()
					thinking = "\n".join(thoughts).strip()
					if not (text or thinking):
						continue
					messages.append({
						"role": "You" if role_raw == "user" else "Assistant",
						"text": text,
						"thinking": thinking,
					})
		except Exception as e:
			log.warning("Command Code: could not read transcript %s: %s", path, e)
			self._dbg("transcript error:", e)
			return []
		return messages

	def _messagesFromState(self, thread_id):
		"""Fallback for threads the CLI has not written a transcript for."""
		thread = self._stateThreads().get(thread_id)
		if not thread:
			return []
		messages = []
		for record in thread.get("messages") or []:
			role_raw = (record.get("role") or "").lower()
			if role_raw not in ("user", "assistant"):
				continue
			text = (record.get("content") or record.get("responseContent") or "").strip()
			if not text:
				parts = []
				for item in record.get("items") or []:
					value = (item.get("text") or "").strip()
					if value:
						parts.append(value)
				text = "\n".join(parts)
			if not text:
				continue
			messages.append({
				"role": "You" if role_raw == "user" else "Assistant",
				"text": text,
				"thinking": "",
			})
		return messages

	# ------------------------------------------------------------------
	# TranscriptBackend hooks
	# ------------------------------------------------------------------

	def loadMessages(self):
		state = self._appState()
		thread_id = state.get("activeThreadId") or ""
		if not thread_id:
			threads = self.loadThreads()
			thread_id = threads[0]["id"] if threads else ""
		if not thread_id:
			return [], ""
		messages = []
		path = self._transcriptPath(thread_id)
		if path:
			messages = self._parseTranscript(path)
		if not messages:
			messages = self._messagesFromState(thread_id)
		self._dbg("loadMessages: thread=%s messages=%d path=%s" % (
			thread_id, len(messages), path or "none"))
		return messages, thread_id

	def loadThreads(self):
		state_threads = self._stateThreads()
		titles = {}
		activity = {}
		for thread_id, thread in state_threads.items():
			titles[thread_id] = (thread.get("title") or "").strip()
			activity[thread_id] = (
				thread.get("lastActivityAt") or thread.get("updatedAt") or 0
			) / 1000.0

		threads = []
		for thread_id, path in self._transcriptPaths():
			try:
				modified = os.path.getmtime(path)
			except OSError:
				continue
			title = titles.get(thread_id) or self._titleFromTranscript(path)
			folder = _folderLabel(self._cwdFromTranscript(path))
			threads.append({
				"id": thread_id,
				"title": title or folder or thread_id,
				"detail": folder,
				"sort": activity.get(thread_id) or modified,
			})
		# Threads the renderer knows about but that have no transcript yet.
		known = {thread["id"] for thread in threads}
		for thread_id, thread in state_threads.items():
			if thread_id in known:
				continue
			threads.append({
				"id": thread_id,
				"title": titles.get(thread_id) or thread_id,
				"detail": _folderLabel(thread.get("projectPath") or ""),
				"sort": activity.get(thread_id) or 0,
			})
		threads.sort(key=lambda thread: thread["sort"], reverse=True)
		return threads

	# ------------------------------------------------------------------
	# Transcript header helpers
	# ------------------------------------------------------------------

	def _headerRecords(self, path):
		"""Yield the first records of a transcript, bounded to a fixed read."""
		read = 0
		try:
			with open(path, encoding="utf-8", errors="replace") as handle:
				for line in handle:
					read += len(line)
					if read > _TITLE_SCAN_BYTES:
						return
					line = line.strip()
					if not line:
						continue
					try:
						yield json.loads(line)
					except ValueError:
						continue
		except Exception:
			return

	def _titleFromTranscript(self, path):
		for record in self._headerRecords(path):
			if record.get("type") != "message":
				continue
			message = record.get("message") or {}
			if (message.get("role") or "").lower() != "user":
				continue
			content = message.get("content")
			if not isinstance(content, list):
				continue
			for part in content:
				if isinstance(part, dict) and part.get("type") == "text":
					text = " ".join((part.get("text") or "").split())
					if text:
						return text[:80]
		return ""

	def _cwdFromTranscript(self, path):
		for record in self._headerRecords(path):
			if record.get("type") == "session":
				return record.get("cwd") or ""
		return ""
