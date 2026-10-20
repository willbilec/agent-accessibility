# Copyright (C) 2026 Will Bishop
# This file is covered by the GNU General Public License.
"""Asynchronous transcript buffers for terminal agents and Antigravity."""

import json
import os
import subprocess
import threading
import time

import ui
import wx
from logHandler import log

from .chatgptBackend import _systemPythonCommand, _subprocessCreationFlags, _subprocessStartupInfo
from .transcriptBackend import TranscriptBackend


SOURCES = (
    ('codexcli', 'Codex CLI', 'codex'),
    ('muse', 'Muse Code in WSL', 'muse'),
)


class AgentTranscriptBackend(TranscriptBackend):
    threadNoun = 'transcript'

    def __init__(self, label, provider):
        self.appLabel = label
        self.logPrefix = provider + 'Transcript'
        super().__init__()
        self.provider = provider
        self._selected = ''
        self._snapshot = {'threads': [], 'messages': [], 'id': ''}
        self._loadedAt = 0.0
        self._busy = False
        self._pendingAction = None

    def loadThreads(self):
        return self._snapshot['threads']

    def loadMessages(self):
        return self._snapshot['messages'], self._snapshot['id']

    def _anchorCursorToText(self, search_text):
        # A buffer can belong to a different terminal tab or background app.
        pass

    def activateThread(self, thread):
        self._selected = thread['id']
        self._loadedAt = 0.0
        if self._busy:
            self._pendingAction = 'lastMessage'
            return True
        self._request('lastMessage')
        return True

    def _request(self, action):
        if not self._running:
            return
        if self._busy:
            ui.message('%s transcripts are loading' % self.appLabel)
            return
        listing = action in ('openSessionPicker', 'nextSession', 'previousSession')
        if not listing and self._loadedAt and time.monotonic() - self._loadedAt < 2:
            getattr(super(), action)()
            return
        self._busy = True
        selected = self._selected
        ui.message('Loading %s transcripts' % self.appLabel)

        def read():
            payload, error = None, None
            try:
                command = _systemPythonCommand()
                if not command:
                    raise RuntimeError('Install Python 3 with SQLite to read agent transcripts.')
                helper = os.path.join(os.path.dirname(__file__), 'agentStores.py')
                process = subprocess.run(
                    command + [helper, self.provider, selected, 'list' if listing else 'messages'],
                    capture_output=True, encoding='utf-8', errors='replace', timeout=60,
                    creationflags=_subprocessCreationFlags(), startupinfo=_subprocessStartupInfo())
                payload = json.loads(process.stdout)
                if process.returncode or payload.get('error'):
                    raise RuntimeError(payload.get('error') or 'Transcript reader failed')
                if not all(isinstance(payload.get(key), list) for key in ('threads', 'messages')):
                    raise ValueError('Transcript reader returned invalid data')
            except Exception as exc:
                log.warning('%s transcript read failed: %s', self.appLabel, exc)
                error = '%s transcripts could not be loaded. %s' % (self.appLabel, exc)
            wx.CallAfter(self._finish, action, listing, payload, error, selected)

        threading.Thread(target=read, daemon=True).start()

    def _finish(self, action, listing, payload, error, requested_selected=None):
        self._busy = False
        if not self._running:
            return
        if requested_selected is not None and requested_selected != self._selected:
            action = self._pendingAction or action
            self._pendingAction = None
            self._request(action)
            return
        if error:
            ui.message(error)
            return
        old_id = self._messagesId
        self._snapshot = payload
        self._threads = payload['threads']
        self._threadsTime = time.monotonic()
        self._threadIdx = next((i for i, thread in enumerate(self._threads)
                                if thread['id'] == self._selected), -1)
        if listing:
            self._loadedAt = 0.0
        if not listing:
            self._loadedAt = time.monotonic()
            self._messages = payload['messages']
            self._messagesTime = self._loadedAt
            self._messagesId = payload['id']
            if old_id != self._messagesId:
                self._msgIndex = -1
            if not self._selected and payload['id']:
                self._selected = payload['id']
                self._threadIdx = next((i for i, thread in enumerate(self._threads)
                                        if thread['id'] == self._selected), -1)
                title = next((t['title'] for t in self._threads if t['id'] == self._selected), self._selected)
                ui.message('Reading newest %s transcript: %s. Use NVDA+Alt+S to select another.' % (
                    self.appLabel, title))
        getattr(super(), action)()

    def nextMessage(self):
        self._request('nextMessage')

    def previousMessage(self):
        self._request('previousMessage')

    def firstMessage(self):
        self._loadedAt = 0.0
        self._request('firstMessage')

    def lastMessage(self):
        self._loadedAt = 0.0
        self._request('lastMessage')

    def readCurrentMessage(self):
        self._request('readCurrentMessage')

    def readThinking(self):
        self._request('readThinking')

    def openSessionPicker(self):
        self._request('openSessionPicker')

    def nextSession(self):
        self._request('nextSession')

    def previousSession(self):
        self._request('previousSession')

    def dumpDebug(self):
        self._request('dumpDebug')
