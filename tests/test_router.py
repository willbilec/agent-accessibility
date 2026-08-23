import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROUTER_PATH = Path(__file__).resolve().parents[1] / "addon" / "globalPlugins" / "addtl" / "router.py"


def _load_router():
    api = types.ModuleType("api")
    api.getForegroundObject = lambda: None
    sys.modules["api"] = api
    spec = importlib.util.spec_from_file_location("agent_accessibility_router_test", ROUTER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _foreground(app_name, app_path, title="", product_name=""):
    return types.SimpleNamespace(
        name=title,
        description="",
        appModule=types.SimpleNamespace(
            appName=app_name,
            appPath=app_path,
            processPath=app_path,
            productName=product_name,
            windowClassName="Chrome_WidgetWin_1",
        ),
    )


class RouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.router = _load_router()

    def setUp(self):
        self.router.reset_cache()

    def test_routes_current_chatgpt_desktop_process(self):
        self.router.api.getForegroundObject = lambda: _foreground(
            "chatgpt",
            r"C:\Program Files\WindowsApps\OpenAI.Codex_26.707.8479.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe",
            title="ChatGPT",
            product_name="ChatGPT",
        )
        self.assertEqual(self.router.route(), "chatgpt")

    def test_routes_legacy_codex_desktop_process(self):
        self.router.api.getForegroundObject = lambda: _foreground(
            "codex",
            r"C:\Program Files\WindowsApps\OpenAI.Codex_1.0\app\Codex.exe",
            title="Codex",
            product_name="Codex",
        )
        self.assertEqual(self.router.route(), "chatgpt")

    def test_does_not_route_chatgpt_browser_tab(self):
        self.router.api.getForegroundObject = lambda: _foreground(
            "msedge",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            title="ChatGPT - Microsoft Edge",
            product_name="Microsoft Edge",
        )
        self.assertIsNone(self.router.route())

    def test_other_backends_keep_precedence(self):
        self.router.api.getForegroundObject = lambda: _foreground(
            "opencode",
            r"C:\Program Files\OpenCode\opencode.exe",
            title="OpenCode",
            product_name="OpenCode",
        )
        self.assertEqual(self.router.route(), "opencode")

    def test_message_commands_fall_back_to_codex_outside_agent_windows(self):
        self.router.api.getForegroundObject = lambda: _foreground(
            "notepad",
            r"C:\Windows\System32\notepad.exe",
            title="Notes",
            product_name="Notepad",
        )
        self.assertEqual(self.router.route_message_command(), "chatgpt")

    def test_message_commands_preserve_foreground_agent_precedence(self):
        self.router.api.getForegroundObject = lambda: _foreground(
            "hermes",
            r"C:\Program Files\Hermes\hermes.exe",
            title="Hermes",
            product_name="Hermes",
        )
        self.assertEqual(self.router.route_message_command(), "hermes")


if __name__ == "__main__":
    unittest.main()
