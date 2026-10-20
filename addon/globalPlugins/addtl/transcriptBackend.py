# -*- coding: UTF-8 -*-
# globalPlugins/addtl/transcriptBackend.py
#
# Shared message-navigation base for the Command Code and Freebuff backends.
#
# Both apps keep a linear transcript on disk that the add-on reads directly:
# Command Code in ~/.commandcode/projects/<slug>/<thread>.jsonl, Freebuff in a
# per-project desktop-v2.db. Everything that does not depend on where the data
# lives — the stable message index, announcement, browse-cursor anchoring,
# thread cycling, the picker and the diagnostic dump — lives here.
#
# Subclasses supply loadMessages() and loadThreads(). Neither app exposes a
# deep link for opening a conversation, so activateThread() falls back to
# activating the matching object in the app's accessibility tree.

import os
import time

import api
import gui
import speech
import textInfos
import ui
import wx
from logHandler import log

_MESSAGES_TTL = 2.0
_THREADS_TTL = 30.0
# An Electron renderer chains deeply — window, pane, group, document, list,
# row is easily fifteen levels — so a shallow cap truncates the walk before it
# ever reaches a thread row. Bound the walk by node count and elapsed time
# instead, which is what actually shows up on a big window, and keep depth as
# a backstop against a pathological cycle.
_TREE_WALK_DEPTH = 25
# Lists in these renderers are large and often virtualized, so a small budget
# silently finds only whatever happens to render early. Walk generously, but
# under a wall-clock ceiling so a huge tree cannot hang NVDA.
_TREE_MAX_NODES = 6000
_TREE_WALK_BUDGET_S = 2.5
_TREE_PARTIAL_LIMIT = 5


class TranscriptBackend(object):

	#: Shown in announcements and diagnostics ("Command Code", "Freebuff").
	appLabel = "Agent"
	#: What the app calls a conversation ("thread", "session").
	threadNoun = "thread"
	#: Prefix for the debug log written by NVDA+Alt+D.
	logPrefix = "agentTranscript"

	def __init__(self, plugin=None):
		self._plugin = plugin
		self._running = True
		self._msgIndex = -1
		self._messages = []
		self._messagesId = ""
		self._messagesTime = 0.0
		self._threads = []
		self._threadsTime = 0.0
		self._threadIdx = -1
		self._debugPath = os.path.join(
			os.path.expanduser("~"), "AppData", "Roaming", "nvda",
			"%s_debug.log" % self.logPrefix,
		)

	def terminate(self):
		self._running = False

	# ------------------------------------------------------------------
	# Hooks for the app-specific subclasses
	# ------------------------------------------------------------------

	def loadMessages(self):
		"""Return (messages, conversationId).

		Each message is {'role': str, 'text': str, 'thinking': str}. The
		conversation id is used to reset the index when the user switches
		conversations in the app itself.
		"""
		raise NotImplementedError

	def loadThreads(self):
		"""Return [{'id', 'title', 'detail'}] newest first."""
		return []

	def activateThread(self, thread):
		"""Switch the running app to `thread`. True if the app was told to."""
		return self._activateByTitle(thread.get("title", ""))

	# ------------------------------------------------------------------
	# Diagnostics
	# ------------------------------------------------------------------

	def _dbg(self, *args):
		try:
			with open(self._debugPath, "a", encoding="utf-8") as handle:
				handle.write(time.strftime("%H:%M:%S") + "  "
					+ "  ".join(str(a) for a in args) + "\n")
		except Exception:
			pass

	# ------------------------------------------------------------------
	# Message cache
	# ------------------------------------------------------------------

	def _invalidateMessages(self):
		self._messagesTime = 0.0
		self._messagesId = ""
		self._msgIndex = -1

	def _getMessages(self, force_refresh=False):
		now = time.monotonic()
		stale = (now - self._messagesTime) >= _MESSAGES_TTL
		if not force_refresh and self._messages and not stale:
			return self._messages
		try:
			messages, conversation_id = self.loadMessages()
		except Exception as e:
			log.warning("%s: could not load messages: %s", self.appLabel, e)
			self._dbg("loadMessages error:", e)
			messages, conversation_id = [], ""
		if messages or force_refresh or not self._messages or (
			conversation_id and conversation_id != self._messagesId
		):
			# A changed conversation ID means the cache now describes a
			# different conversation; replace it even when the new
			# conversation has no messages yet, or navigation would keep
			# announcing the previous conversation's messages.
			self._messages = messages
			self._messagesTime = now
		if conversation_id and conversation_id != self._messagesId:
			# The user switched conversations in the app; the old index
			# points at nothing meaningful any more.
			self._messagesId = conversation_id
			self._msgIndex = -1
			self._dbg("conversation changed:", conversation_id)
		return self._messages

	# ------------------------------------------------------------------
	# Browse-cursor anchoring
	# ------------------------------------------------------------------

	def _getRawTreeInterceptor(self):
		try:
			focus = api.getFocusObject()
		except Exception:
			return None
		if focus is None:
			return None
		return getattr(focus, "treeInterceptor", None)

	def _anchorCursorToText(self, search_text):
		if not search_text:
			return
		ti = self._getRawTreeInterceptor()
		if ti is None:
			return
		needle = search_text[:60].strip()
		if not needle:
			return
		try:
			info = ti.makeTextInfo(textInfos.POSITION_FIRST)
			if info.find(needle, caseSensitive=False):
				info.collapse()
				try:
					ti.selection = info
				except Exception:
					pass
		except Exception:
			pass

	def _announceAndAnchor(self, msg):
		speech.cancelSpeech()
		thinking = (msg.get("thinking") or "").strip()
		if thinking:
			ui.message("%s: %s\nThinking: %s" % (msg["role"], msg["text"], thinking))
		else:
			ui.message("%s: %s" % (msg["role"], msg["text"]))
		self._anchorCursorToText(msg["text"])

	# ------------------------------------------------------------------
	# Message navigation
	# ------------------------------------------------------------------

	def nextMessage(self):
		msgs = self._getMessages()
		if not msgs:
			ui.message("No messages found")
			return
		nxt = self._msgIndex + 1
		if nxt >= len(msgs):
			ui.message("No more messages")
			return
		self._msgIndex = nxt
		self._announceAndAnchor(msgs[self._msgIndex])

	def previousMessage(self):
		msgs = self._getMessages()
		if not msgs:
			ui.message("No messages found")
			return
		nxt = self._msgIndex - 1
		if nxt < 0:
			ui.message("Already at first message")
			return
		self._msgIndex = nxt
		self._announceAndAnchor(msgs[self._msgIndex])

	def firstMessage(self):
		msgs = self._getMessages(force_refresh=True)
		if not msgs:
			ui.message("No messages found")
			return
		self._msgIndex = 0
		self._announceAndAnchor(msgs[0])

	def lastMessage(self):
		msgs = self._getMessages(force_refresh=True)
		if not msgs:
			ui.message("No messages found")
			return
		self._msgIndex = len(msgs) - 1
		self._announceAndAnchor(msgs[-1])

	def readCurrentMessage(self):
		msgs = self._getMessages()
		if not (0 <= self._msgIndex < len(msgs)):
			ui.message("No message selected. Press NVDA+Alt+Down to start.")
			return
		msg = msgs[self._msgIndex]
		speech.cancelSpeech()
		ui.message("%s: %s" % (msg["role"], msg["text"]))

	def readThinking(self):
		msgs = self._getMessages()
		if not (0 <= self._msgIndex < len(msgs)):
			ui.message("No message selected. Press NVDA+Alt+Down to start.")
			return
		msg = msgs[self._msgIndex]
		if msg["role"] != "Assistant":
			ui.message("Current message is not from the assistant.")
			return
		thinking = (msg.get("thinking") or "").strip()
		if thinking:
			ui.message("Thinking: %s" % thinking)
		else:
			ui.message("No thinking available for this message.")

	# ------------------------------------------------------------------
	# Conversation switching
	# ------------------------------------------------------------------

	def _threadLabel(self, thread):
		title = (thread.get("title") or "").strip() or "(untitled)"
		detail = (thread.get("detail") or "").strip()
		if detail and detail.lower() != title.lower():
			return "%s  (%s)" % (title, detail)
		return title

	def _refreshThreads(self, force=False):
		now = time.monotonic()
		if not force and self._threads and (now - self._threadsTime) < _THREADS_TTL:
			return
		try:
			self._threads = self.loadThreads() or []
		except Exception as e:
			log.warning("%s: could not list threads: %s", self.appLabel, e)
			self._dbg("loadThreads error:", e)
			self._threads = []
		self._threadsTime = now
		if self._threadIdx >= len(self._threads):
			self._threadIdx = -1
		self._dbg("thread cache: %d entries" % len(self._threads))

	def _jumpToThread(self):
		if not (0 <= self._threadIdx < len(self._threads)):
			return
		thread = self._threads[self._threadIdx]
		label = self._threadLabel(thread)
		if self.activateThread(thread):
			self._invalidateMessages()
			ui.message("[%d/%d] %s" % (self._threadIdx + 1, len(self._threads), label))
		else:
			ui.message("Could not switch to %s in %s. Bring it into view in the %s list and try again." % (
				label, self.appLabel, self.threadNoun))

	def nextSession(self):
		self._refreshThreads()
		if not self._threads:
			ui.message("No %ss found in %s" % (self.threadNoun, self.appLabel))
			return
		self._threadIdx = (self._threadIdx + 1) % len(self._threads)
		self._jumpToThread()

	def previousSession(self):
		self._refreshThreads()
		if not self._threads:
			ui.message("No %ss found in %s" % (self.threadNoun, self.appLabel))
			return
		if self._threadIdx <= 0:
			self._threadIdx = len(self._threads) - 1
		else:
			self._threadIdx -= 1
		self._jumpToThread()

	def openSessionPicker(self):
		self._refreshThreads(force=True)
		if not self._threads:
			ui.message("No %ss found in %s" % (self.threadNoun, self.appLabel))
			return
		labels = [self._threadLabel(thread) for thread in self._threads]

		def _show():
			gui.mainFrame.prePopup()
			dlg = wx.SingleChoiceDialog(
				gui.mainFrame,
				"Select a %s to open" % self.threadNoun,
				"%s %ss" % (self.appLabel, self.threadNoun.capitalize()),
				labels,
			)
			result = dlg.ShowModal()
			idx = dlg.GetSelection()
			dlg.Destroy()
			gui.mainFrame.postPopup()
			if result == wx.ID_OK and 0 <= idx < len(self._threads):
				self._threadIdx = idx
				self._jumpToThread()

		wx.CallAfter(_show)

	@staticmethod
	def _isHeading(obj):
		"""True when an object is a heading rather than a list row.

		An open conversation exposes its own title as a heading. Activating
		that would report success while changing nothing, so headings are
		never matched.
		"""
		try:
			text = str(getattr(obj, "roleDisplayString", "") or "").lower()
		except Exception:
			return False
		return "heading" in text

	def _collectTreeObjects(self, title):
		"""Return accessible objects in the foreground window whose name is `title`.

		Exact matches win; otherwise the first substring matches are used.
		The walk is bounded by depth, node count and elapsed time, and the
		result is logged so a failed switch can be told apart from a lookup
		that never reached that part of the tree.
		"""
		needle = (title or "").strip().lower()
		if not needle:
			return []
		try:
			root = api.getForegroundObject()
		except Exception:
			return []
		if root is None:
			return []
		exact = []
		partial = []
		budget = [time.monotonic() + _TREE_WALK_BUDGET_S]
		counter = [0, False]
		deepest = [0]
		depth_capped = [False]

		def expired():
			if counter[0] >= _TREE_MAX_NODES or time.monotonic() > budget[0]:
				counter[1] = True
				return True
			return False

		def walk(obj, depth):
			if counter[1]:
				return
			if depth > _TREE_WALK_DEPTH:
				depth_capped[0] = True
				return
			if depth > deepest[0]:
				deepest[0] = depth
			try:
				children = list(obj.children)
			except Exception:
				return
			for child in children:
				counter[0] += 1
				if expired():
					return
				try:
					name = (child.name or "").strip().lower()
				except Exception:
					name = ""
				if name and not self._isHeading(child):
					if name == needle:
						exact.append(child)
					elif needle in name and len(partial) < _TREE_PARTIAL_LIMIT:
						partial.append(child)
				walk(child, depth + 1)

		try:
			walk(root, 0)
		except Exception as e:
			self._dbg("lookup %r: walk error %s" % (title, e))
		# The stop reason matters: a shallow result that was never capped means
		# the object is not in the tree the reader can see, which no budget can
		# fix, while a capped walk may still have the target further in.
		if counter[1]:
			stopped = " (stopped: node/time budget)"
		elif depth_capped[0]:
			stopped = " (stopped: depth %d)" % _TREE_WALK_DEPTH
		else:
			stopped = " (whole tree)"
		self._dbg("lookup %r: %d nodes, depth %d, %d exact, %d partial%s" % (
			title, counter[0], deepest[0], len(exact), len(partial), stopped))
		return exact or partial

	def _activateByTitle(self, title):
		"""Activate the app's own control for `title`.

		Command Code and Freebuff register no deep link that opens a
		conversation, so the only way the add-on can switch is to trigger
		the matching list item in the renderer.
		"""
		matches = self._collectTreeObjects(title)
		for obj in matches:
			try:
				obj.doAction()
				self._dbg("activate: doAction on %r" % title)
				return True
			except Exception as e:
				self._dbg("activate: doAction failed on %r: %s" % (title, e))
		for obj in matches:
			try:
				obj.setFocus()
				self._dbg("activate: setFocus on %r" % title)
				return True
			except Exception:
				continue
		return False

	# ------------------------------------------------------------------
	# Diagnostic dump
	# ------------------------------------------------------------------

	def dumpDebug(self):
		self._dbg("=== DUMP %s ===" % self.appLabel)
		try:
			fg = api.getForegroundObject()
			self._dbg("foreground=%r appModule=%r" % (
				getattr(fg, "name", ""),
				getattr(getattr(fg, "appModule", None), "appName", "")))
		except Exception as e:
			self._dbg("foreground error:", e)
		msgs = self._getMessages(force_refresh=True)
		self._dbg("messages: %d (index %d, conversation %s)" % (
			len(msgs), self._msgIndex, self._messagesId or "unknown"))
		for i, msg in enumerate(msgs[:12]):
			has_thinking = "yes" if msg.get("thinking") else "no"
			self._dbg("  [%d] %s (thinking=%s): %r" % (
				i, msg["role"], has_thinking, msg["text"][:80]))
		self._refreshThreads(force=True)
		self._dbg("threads: %d" % len(self._threads))
		for i, thread in enumerate(self._threads[:12]):
			self._dbg("  {%d} %s" % (i, self._threadLabel(thread)))
		ui.message("%s debug log written: %s" % (self.appLabel, self._debugPath))
