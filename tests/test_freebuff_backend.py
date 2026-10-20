import importlib.util
import sys
import threading
import types
import unittest
from pathlib import Path


ADDTL = Path(__file__).resolve().parents[1] / "addon" / "globalPlugins" / "addtl"
SPOKEN = []
CALL_AFTER = []


def _load_backend():
	package = "freebuffBackend_test_pkg"
	if package + ".freebuffBackend" in sys.modules:
		return sys.modules[package + ".freebuffBackend"]

	stubs = {
		"api": {"getFocusObject": lambda: None, "getForegroundObject": lambda: None},
		"gui": {"mainFrame": None},
		"speech": {"cancelSpeech": lambda: None},
		"textInfos": {"POSITION_FIRST": "first"},
		"ui": {"message": SPOKEN.append},
		"wx": {"CallAfter": lambda fn, *args: CALL_AFTER.append((fn, args))},
		"logHandler": {"log": types.SimpleNamespace(
			info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None)},
	}
	for name, attrs in stubs.items():
		module = types.ModuleType(name)
		module.__dict__.update(attrs)
		sys.modules[name] = module

	pkg = types.ModuleType(package)
	pkg.__path__ = [str(ADDTL)]
	sys.modules[package] = pkg
	for name in ("transcriptBackend", "freebuffBackend"):
		spec = importlib.util.spec_from_file_location(package + "." + name, ADDTL / (name + ".py"))
		module = importlib.util.module_from_spec(spec)
		sys.modules[spec.name] = module
		spec.loader.exec_module(module)
	return sys.modules[package + ".freebuffBackend"]


class FreebuffBackendTests(unittest.TestCase):

	def setUp(self):
		self.module = _load_backend()
		SPOKEN.clear()
		CALL_AFTER.clear()
		self.backend = self.module.FreebuffBackend()
		self.backend._dbg = lambda *_args: None
		self.release = threading.Event()
		self.read_threads = []

		def slow_read():
			self.read_threads.append(threading.get_ident())
			self.release.wait(5)
			return [{"role": "User", "text": "hello", "thinking": ""}], "t1"

		self.backend._readMessages = slow_read

	def _drain(self, worker_count):
		self.release.set()
		for thread in threading.enumerate():
			if thread is not threading.current_thread() and thread.daemon:
				thread.join(5)
		self.assertEqual(len(CALL_AFTER), worker_count)
		for fn, args in CALL_AFTER:
			fn(*args)
		CALL_AFTER.clear()

	def test_a_gesture_never_waits_for_the_store(self):
		self.backend.nextMessage()
		# The read is still blocked, yet the gesture has already returned.
		self.assertEqual(SPOKEN, ["Loading Freebuff"])
		self.backend.nextMessage()
		self.assertEqual(SPOKEN[-1], "Freebuff is still loading")
		self._drain(1)
		self.assertNotIn(threading.get_ident(), self.read_threads)
		self.assertEqual(SPOKEN[-1], "User: hello")

	def test_a_fresh_snapshot_is_used_without_another_read(self):
		self.backend.nextMessage()
		self._drain(1)
		self.backend.readCurrentMessage()
		self.assertEqual(CALL_AFTER, [])
		self.assertEqual(len(self.read_threads), 1)
		self.assertEqual(SPOKEN[-1], "User: hello")

	def test_a_failed_read_reports_no_messages(self):
		def broken():
			raise OSError("store locked")

		self.backend._readMessages = broken
		self.backend.nextMessage()
		self._drain(1)
		self.assertEqual(SPOKEN[-1], "No messages found")


if __name__ == "__main__":
	unittest.main()
