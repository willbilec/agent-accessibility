import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1] / 'addon/globalPlugins'


class DispatcherTranscriptTests(unittest.TestCase):
    def setUp(self):
        package = types.ModuleType('dispatcher_test')
        package.__path__ = [str(ROOT)]
        addtl = types.ModuleType('dispatcher_test.addtl')
        addtl.__path__ = [str(ROOT / 'addtl')]
        modules = {'dispatcher_test': package, 'dispatcher_test.addtl': addtl}
        for name in ('ui', 'logHandler', 'globalPluginHandler', 'scriptHandler'):
            modules[name] = types.ModuleType(name)
        modules['logHandler'].log = mock.Mock()
        modules['globalPluginHandler'].GlobalPlugin = type('GlobalPlugin', (), {})
        modules['scriptHandler'].script = lambda **kwargs: lambda function: function
        modules['scriptHandler'].getLastScriptRepeatCount = lambda: 0
        for filename, classname in [('hermesBackend', 'HermesBackend'), ('opencodeBackend', 'OpenCodeBackend'),
                                    ('chatgptBackend', 'ChatGPTBackend'), ('commandCodeBackend', 'CommandCodeBackend'),
                                    ('freebuffBackend', 'FreebuffBackend'),
                                    ('agentTranscriptBackend', 'AgentTranscriptBackend')]:
            name = 'dispatcher_test.addtl.' + filename
            modules[name] = types.ModuleType(name)
            setattr(modules[name], classname, mock.Mock())
        modules['dispatcher_test.addtl.agentTranscriptBackend'].SOURCES = ()
        router = types.ModuleType('dispatcher_test.addtl.router')
        router.route = mock.Mock(return_value=None)
        modules[router.__name__] = router
        with mock.patch.dict(sys.modules, modules):
            spec = importlib.util.spec_from_file_location('dispatcher_test.plugin', ROOT / 'agentDesktopAccessibility.py')
            self.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.module)
        self.plugin = self.module.GlobalPlugin.__new__(self.module.GlobalPlugin)
        self.plugin._transcripts = {key: mock.Mock() for key in
                                    ('codexcli',)}
        self.plugin._transcriptSource = 'codexcli'
        self.plugin._chatgpt = mock.Mock()
        self.gesture = mock.Mock()

    def test_every_source_gets_shared_commands(self):
        for key, backend in self.plugin._transcripts.items():
            self.module.route.return_value = key
            for name, action in [('nextMessage', 'nextMessage'), ('previousMessage', 'previousMessage'),
                                 ('firstMessage', 'firstMessage'), ('lastMessage', 'lastMessage'),
                                 ('readCurrentMessage', 'readCurrentMessage'), ('sessionPicker', 'openSessionPicker'),
                                 ('nextSession', 'nextSession'), ('previousSession', 'previousSession'),
                                 ('dump', 'dumpDebug'), ('readThinking', 'readThinking')]:
                getattr(self.plugin, 'script_' + name)(self.gesture)
                getattr(backend, action).assert_called_once_with()
            backend.reset_mock()
        self.plugin._chatgpt.assert_not_called()
        self.gesture.send.assert_not_called()

    def test_selected_background_source_and_focused_desktop_precedence(self):
        self.plugin.script_nextMessage(self.gesture)
        self.plugin._transcripts['codexcli'].nextMessage.assert_called_once_with()
        self.module.route.return_value = 'chatgpt'
        self.plugin.script_nextMessage(self.gesture)
        self.plugin._chatgpt.nextMessage.assert_called_once_with()
        self.plugin._transcriptSource = None
        self.module.route.return_value = None
        self.plugin.script_firstMessage(self.gesture)
        self.plugin._chatgpt.firstMessage.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
