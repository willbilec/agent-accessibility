import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


HELPER = Path(__file__).resolve().parents[1] / 'addon/globalPlugins/addtl/agentStores.py'


def load_helper():
    spec = importlib.util.spec_from_file_location('agent_stores_test', HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = load_helper()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)


    def test_codex_reads_migrated_history_and_excludes_subagents(self):
        root = self.home / '.codex'
        root.mkdir()
        with sqlite3.connect(root / 'state_5.sqlite') as db:
            db.execute('CREATE TABLE threads (id TEXT, title TEXT, cwd TEXT, updated_at INTEGER, '
                       'rollout_path TEXT, source TEXT, archived INTEGER)')
            db.executemany('INSERT INTO threads VALUES (?,?,?,?,?,?,?)', [
                ('main', 'Main', '/project', 2, '', 'cli', 0),
                ('child', 'Child', '/project', 3, '', '{"subagent":{}}', 0),
                ('old', 'Old', '/project', 1, '', 'cli', 1)])
        db.close()
        with sqlite3.connect(root / 'thread_history_1.sqlite') as db:
            db.execute('CREATE TABLE thread_items (thread_id TEXT, rollout_ordinal INTEGER, item_json TEXT)')
            db.executemany('INSERT INTO thread_items VALUES (?,?,?)', [
                ('main', 2, json.dumps({'type': 'agentMessage', 'text': 'Answer'})),
                ('main', 1, json.dumps({'type': 'userMessage', 'content': [{'text': 'Question'}]}))])
        db.close()
        sessions = root / 'sessions'
        sessions.mkdir()
        for sid in ('old', 'child'):
            (sessions / (sid + '.jsonl')).write_text(json.dumps({
                'type': 'session_meta', 'payload': {'id': sid, 'source': 'cli'}}), encoding='utf-8')
        threads = self.store.local_threads('codex', self.home)
        self.assertEqual([t['id'] for t in threads], ['main'])
        self.assertEqual([m['text'] for m in self.store.local_messages('codex', threads[0])],
                         ['Question', 'Answer'])
        baseline = self.store.codex_changes(self.home, None)
        self.assertEqual(baseline['items'], [])
        with sqlite3.connect(root / 'thread_history_1.sqlite') as db:
            db.execute('INSERT INTO thread_items VALUES (?,?,?)',
                       ('main', 3, json.dumps({'type': 'agentMessage', 'text': 'Finished', 'phase': 'final_answer'})))
        db.close()
        updates = self.store.codex_changes(self.home, baseline['cursors'])
        self.assertEqual([item['text'] for item in updates['items']], ['Finished'])
        self.assertEqual(updates['items'][0]['phase'], 'final_answer')
        self.assertEqual(self.store.codex_changes(self.home, updates['cursors'])['items'], [])

    def test_muse_reads_run_events_without_tool_or_reminder_text(self):
        path = self.home / 'session.jsonl'
        events = [
            {'kind': 'started', 'prompt': 'Question'},
            {'kind': 'assistant_message_committed', 'text': 'Answer'},
            {'kind': 'tool_result_batch_committed', 'text': 'Hidden'},
            {'kind': 'inbox_item_queued', 'source': 'user_steer', 'payload': {'prompt': 'More'}},
        ]
        path.write_text('\n'.join(json.dumps({'payload': {'kind': 'run', 'event': e}})
                                  for e in events), encoding='utf-8')
        self.assertEqual([m['text'] for m in self.store.muse_messages(path)],
                         ['Question', 'Answer', 'More'])

    def test_codex_streaming_completion_is_read_when_ordinal_does_not_change(self):
        root = self.home / '.codex'
        root.mkdir()
        with sqlite3.connect(root / 'state_5.sqlite') as db:
            db.execute('CREATE TABLE threads (id TEXT, title TEXT, cwd TEXT, updated_at INTEGER, '
                       'rollout_path TEXT, source TEXT, archived INTEGER)')
            db.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?)', ('main', 'Main', '', 1, '', 'cli', 0))
        db.close()
        with sqlite3.connect(root / 'thread_history_1.sqlite') as db:
            db.execute('CREATE TABLE thread_items (thread_id TEXT, rollout_ordinal INTEGER, '
                       'updated_at_ordinal INTEGER, completed_at_ms INTEGER, item_json TEXT)')
            db.execute('INSERT INTO thread_items VALUES (?,?,?,?,?)', ('main', 1, 1, None,
                       json.dumps({'type': 'agentMessage', 'text': 'Partial', 'phase': 'final_answer'})))
        db.close()
        baseline = self.store.codex_changes(self.home, None)
        waiting = self.store.codex_changes(self.home, baseline['cursors'])
        self.assertEqual(waiting['items'], [])
        with sqlite3.connect(root / 'thread_history_1.sqlite') as db:
            db.execute('UPDATE thread_items SET completed_at_ms=2,item_json=?',
                       (json.dumps({'type': 'agentMessage', 'text': 'Complete', 'phase': 'final_answer'}),))
        db.close()
        finished = self.store.codex_changes(self.home, waiting['cursors'])
        self.assertEqual([m['text'] for m in finished['items']], ['Complete'])
        self.assertEqual(self.store.codex_changes(self.home, finished['cursors'])['items'], [])


    def test_selected_missing_thread_never_falls_back_to_another_chat(self):
        self.assertIsNone(self.store.select_thread([{'id': 'other'}], 'missing'))

    def test_codex_threads_support_updated_at_ms_schema(self):
        root = self.home / '.codex'
        root.mkdir()
        with sqlite3.connect(root / 'state_9.sqlite') as db:
            db.execute('CREATE TABLE threads (id TEXT, title TEXT, cwd TEXT, updated_at_ms INTEGER, '
                       'rollout_path TEXT, source TEXT, archived INTEGER)')
            db.executemany('INSERT INTO threads VALUES (?,?,?,?,?,?,?)', [
                ('older', 'Older', '/project', 1000, '', 'cli', 0),
                ('newer', 'Newer', '/project', 2000, '', 'cli', 0)])
        db.close()
        threads = self.store.local_threads('codex', self.home)
        self.assertEqual([t['id'] for t in threads], ['newer', 'older'])


if __name__ == '__main__':
    unittest.main()
