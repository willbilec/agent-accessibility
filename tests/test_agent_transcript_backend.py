import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1] / 'addon/globalPlugins'


def load_backend():
    package = types.ModuleType('transcript_test')
    package.__path__ = [str(ROOT / 'addtl')]
    dependencies = {name: types.ModuleType(name) for name in
                    ('api', 'gui', 'speech', 'textInfos', 'ui', 'wx', 'logHandler')}
    dependencies['ui'].message = mock.Mock()
    dependencies['speech'].cancelSpeech = mock.Mock()
    dependencies['logHandler'].log = mock.Mock()
    dependencies['wx'].CallAfter = mock.Mock()
    codex = types.ModuleType('transcript_test.chatgptBackend')
    codex._systemPythonCommand = lambda: ['python']
    codex._subprocessCreationFlags = lambda: 0
    codex._subprocessStartupInfo = lambda: None
    dependencies.update({'transcript_test': package, 'transcript_test.chatgptBackend': codex})
    with mock.patch.dict(sys.modules, dependencies):
        spec = importlib.util.spec_from_file_location('transcript_test.agentTranscriptBackend',
                                                     ROOT / 'addtl/agentTranscriptBackend.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class TranscriptBackendTests(unittest.TestCase):
    def setUp(self):
        self.module = load_backend()
        self.backend = self.module.AgentTranscriptBackend('Muse', 'muse')

    def test_reads_off_thread_then_delivers_speech_on_wx_thread(self):
        payload = {'threads': [{'id': 'one', 'title': 'One'}], 'id': 'one',
                   'messages': [{'role': 'Assistant', 'text': 'Answer', 'thinking': ''}]}
        with mock.patch.object(self.module.threading, 'Thread') as thread, \
                mock.patch.object(self.module.subprocess, 'run', return_value=types.SimpleNamespace(
                    stdout=json.dumps(payload), returncode=0)) as run:
            self.backend.nextMessage()
            run.assert_not_called()
            self.assertTrue(self.backend._busy)
            thread.call_args.kwargs['target']()
            self.module.wx.CallAfter.assert_called_once()
            self.assertEqual(self.backend._messages, [])
            callback, *args = self.module.wx.CallAfter.call_args.args
            callback(*args)
        self.assertFalse(self.backend._busy)
        self.assertEqual(self.backend._selected, 'one')
        self.module.ui.message.assert_called_with('Assistant: Answer')

    def test_missing_selected_transcript_clears_old_message_buffer(self):
        self.backend._messages = [{'role': 'Assistant', 'text': 'Old'}]
        self.backend._messagesId = 'old'
        self.backend._selected = 'old'
        self.backend._finish('nextMessage', False,
                             {'threads': [], 'messages': [], 'id': ''}, None)
        self.assertEqual(self.backend._messages, [])
        self.module.ui.message.assert_called_with('No messages found')

    def test_read_error_and_shutdown_never_replay_previous_buffer(self):
        self.backend._busy = True
        self.backend._finish('nextMessage', False, None, 'Read failed')
        self.module.ui.message.assert_called_once_with('Read failed')
        self.backend.terminate()
        self.module.ui.message.reset_mock()
        self.backend._finish('nextMessage', False, {}, None)
        self.module.ui.message.assert_not_called()

    def test_selected_transcript_never_activates_an_app_control(self):
        with mock.patch.object(self.backend, '_request') as request:
            self.assertTrue(self.backend.activateThread({'id': 'chosen'}))
        self.assertEqual(self.backend._selected, 'chosen')
        request.assert_called_once_with('lastMessage')

    def test_selection_during_read_rejects_old_payload_and_loads_new_selection(self):
        self.backend._selected = 'old'
        self.backend._busy = True
        self.backend.activateThread({'id': 'new'})
        with mock.patch.object(self.backend, '_request') as request:
            self.backend._finish('nextMessage', False,
                                 {'threads': [], 'messages': [], 'id': 'old'}, None, 'old')
        request.assert_called_once_with('lastMessage')
        self.assertEqual(self.backend._selected, 'new')
        self.assertEqual(self.backend._snapshot['id'], '')

    def test_cycle_resolves_selected_identity_after_reorder(self):
        self.backend._selected = 'b'
        self.backend._threadIdx = 1
        payload = {'threads': [{'id': sid, 'title': sid} for sid in ('c', 'a', 'b')],
                   'id': 'b', 'messages': []}
        with mock.patch.object(self.backend, 'activateThread', return_value=True) as activate:
            self.backend._finish('nextSession', True, payload, None, 'b')
        self.assertEqual(activate.call_args.args[0]['id'], 'c')


if __name__ == '__main__':
    unittest.main()
