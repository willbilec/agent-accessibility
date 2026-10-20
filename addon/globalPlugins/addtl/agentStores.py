# Copyright (C) 2026 Will Bishop
# This file is covered by the GNU General Public License.
"""Read-only transcript helper, executed by system Python (or WSL Python)."""

import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def connect(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1)


def records(path):
    with open(path, encoding='utf-8', errors='replace') as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue  # The writer may still be appending the final line.
            if isinstance(record, dict):
                yield record


def content_text(content):
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ''
    return '\n'.join(p['text'].strip() for p in content if isinstance(p, dict)
                     and p.get('type', 'text') in ('text', 'input_text', 'output_text')
                     and isinstance(p.get('text'), str) and p['text'].strip())


def message(role, text, thinking=''):
    return {'role': 'You' if role == 'user' else 'Assistant', 'text': text, 'thinking': thinking}


def jsonl_messages(provider, path):
    messages = []
    for record in records(path):
        if record.get('isSidechain') or record.get('isMeta'):
            continue
        kind = record.get('type')
        if provider == 'codex':
            part = record.get('payload') if kind == 'response_item' else None
            if not isinstance(part, dict) or part.get('type') != 'message':
                continue
        else:
            if kind not in (('user', 'assistant') if provider == 'claude' else ('message',)):
                continue
            part = record.get('message')
        if not isinstance(part, dict) or part.get('role') not in ('user', 'assistant'):
            continue
        content = part.get('content')
        text = content_text(content)
        thinking = '\n'.join(str(p.get('thinking') or p.get('text') or '').strip()
                             for p in content if isinstance(p, dict)
                             and p.get('type') in ('thinking', 'reasoning')) if isinstance(content, list) else ''
        if text or thinking:
            messages.append(message(part['role'], text, thinking))
    return messages


def local_threads(provider, home=None):
    home = Path(home or Path.home())
    threads = []
    if provider == 'codex':
        root = home / '.codex'
        known = set()
        databases = sorted(root.glob('state_*.sqlite'), key=lambda p: p.stat().st_mtime, reverse=True)
        if databases:
            with closing(connect(databases[0])) as db:
                db.row_factory = sqlite3.Row
                # Current Codex builds name the recency column differently
                # (updated_at_ms / recency_at_ms); use whichever the schema
                # actually has, like chatgptBackend._loadCodexTasks does.
                columns = {r[1] for r in db.execute('PRAGMA table_info(threads)')}
                recency = next((name for name in ('recency_at_ms', 'updated_at_ms', 'recency_at',
                                                  'updated_at', 'created_at_ms', 'created_at')
                                if name in columns), None)
                query = 'SELECT * FROM threads'
                if recency:
                    query += ' ORDER BY %s DESC' % recency
                for row in db.execute(query):
                    row = dict(row)
                    known.add(row['id'])
                    if row.get('archived') or str(row.get('source', '')).startswith('{') or row.get('thread_source') == 'subagent':
                        continue
                    threads.append({'id': row['id'], 'title': row.get('name') or row['title'] or row['id'],
                                    'detail': row.get('cwd') or '', 'path': row.get('rollout_path') or '',
                                    'home': str(home), 'sort': row.get(recency) or 0})
        for path in root.glob('sessions/**/*.jsonl'):
            header = next(records(path), {})
            meta = header.get('payload') or {}
            sid = meta.get('id')
            source = meta.get('source')
            if not sid or sid in known or isinstance(source, dict):
                continue
            threads.append({'id': sid, 'title': sid, 'detail': meta.get('cwd', ''),
                            'path': str(path), 'home': str(home), 'sort': path.stat().st_mtime})
    else:
        raise ValueError("Unknown local transcript source")
    return sorted(threads, key=lambda t: t['sort'], reverse=True)


def local_messages(provider, thread):
    if provider == 'codex':
        root = Path(thread['home']) / '.codex'
        databases = sorted(root.glob('thread_history_*.sqlite'), key=lambda p: p.stat().st_mtime, reverse=True)
        if databases:
            with closing(connect(databases[0])) as db:
                messages = []
                for raw, in db.execute('SELECT item_json FROM thread_items WHERE thread_id=? '
                                      'ORDER BY rollout_ordinal', (thread['id'],)):
                    try:
                        item = json.loads(raw)
                    except ValueError:
                        continue
                    kind = item.get('type')
                    if kind not in ('userMessage', 'agentMessage'):
                        continue
                    text = content_text(item.get('content')) if kind == 'userMessage' else item.get('text', '')
                    if isinstance(text, str) and text.strip():
                        messages.append(message('user' if kind == 'userMessage' else 'assistant', text))
                if messages:
                    return messages
    return jsonl_messages(provider, thread['path']) if os.path.isfile(thread['path']) else []


def muse_messages(path):
    messages = []
    for record in records(path):
        payload = record.get('payload') or {}
        if not isinstance(payload, dict):
            continue
        event = payload.get('event') or {}
        if not isinstance(event, dict):
            continue
        kind = event.get('kind')
        role, text = '', ''
        if payload.get('kind') == 'run' and kind == 'started':
            role, text = 'user', event.get('prompt')
        elif payload.get('kind') == 'run' and kind == 'assistant_message_committed':
            role, text = 'assistant', event.get('text')
        elif kind == 'inbox_item_queued':
            source = event.get('source')
            if source == 'user_steer' or isinstance(source, dict) and source.get('source') == 'user_steer':
                inner = event.get('payload') or {}
                role, text = 'user', inner.get('prompt') or event.get('body')
        if isinstance(text, str) and text.strip():
            messages.append(message(role, text))
    return messages


def muse_threads():
    root = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'muse'
    with closing(connect(root / 'session-index.db')) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('SELECT session_id, title, workspace_root, session_log_path FROM sessions '
                          'WHERE prompt_count > 0 ORDER BY updated_at_us DESC LIMIT 500')
        return [{'id': r['session_id'], 'title': r['title'] or r['session_id'],
                 'detail': r['workspace_root'] or '', 'path': r['session_log_path']} for r in rows]











def select_thread(threads, selected):
    if selected:
        return next((t for t in threads if t['id'] == selected), None)
    return threads[0] if threads else None


def codex_changes(home, cursors):
    """Baseline migrated tasks, then return each committed update once."""
    home = Path(home)
    paths = sorted((home / '.codex').glob('thread_history_*.sqlite'),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not paths:
        return {'cursors': cursors, 'items': []}
    result, items = {}, []
    with closing(connect(paths[0])) as db:
        columns = {r[1] for r in db.execute('PRAGMA table_info(thread_items)')}
        ordinal = 'updated_at_ordinal' if 'updated_at_ordinal' in columns else 'rollout_ordinal'
        for thread in local_threads('codex', home):
            if os.path.isfile(thread['path']):
                continue  # The existing JSONL monitor owns unmigrated tasks.
            sid = thread['id']
            high = db.execute('SELECT MAX(%s) FROM thread_items WHERE thread_id=?' % ordinal,
                              (sid,)).fetchone()[0] or 0
            previous = (cursors or {}).get(sid, {'ordinal': 0, 'pending': []})
            pending = previous['pending']
            result[sid] = {'ordinal': high, 'pending': []}
            if cursors is None or high < previous['ordinal']:
                if 'completed_at_ms' in columns:
                    result[sid]['pending'] = [row[0] for row in db.execute(
                        'SELECT rollout_ordinal FROM thread_items WHERE thread_id=? AND completed_at_ms IS NULL',
                        (sid,))]
                continue
            where = 'thread_id=? AND ((%s>? AND %s<=?)' % (ordinal, ordinal)
            parameters = [sid, previous['ordinal'], high]
            if pending:
                where += ' OR rollout_ordinal IN (%s)' % ','.join('?' for _ in pending)
                parameters.extend(pending)
            where += ')'
            completed = 'completed_at_ms' if 'completed_at_ms' in columns else '1'
            for position, done, raw in db.execute(
                    'SELECT rollout_ordinal, %s, item_json FROM thread_items WHERE ' % completed + where +
                    ' ORDER BY rollout_ordinal', parameters):
                if done is None:
                    # Completion can update a row without changing either ordinal.
                    result[sid]['pending'].append(position)
                    continue
                try:
                    item = json.loads(raw)
                except ValueError:
                    continue
                kind = item.get('type')
                if kind == 'agentMessage':
                    role, text = 'assistant', item.get('text')
                elif kind == 'reasoning':
                    role = 'reasoning'
                    summary = item.get('summary') or []
                    text = '\n'.join(s if isinstance(s, str) else s.get('text', '')
                                     for s in summary if isinstance(s, (str, dict)))
                else:
                    continue
                if isinstance(text, str) and text.strip():
                    items.append({'task': thread, 'role': role, 'text': text, 'phase': item.get('phase')})
    return {'cursors': result, 'items': items}


def snapshot(provider, selected="", listing=False):
    if provider in ('codex',):
        threads = local_threads(provider)
    elif provider == 'muse':
        threads = muse_threads()
    else:
        raise ValueError('Unknown transcript source')
    thread = select_thread(threads, selected)
    messages = []
    if thread and not listing:
        if provider in ('codex',):
            messages = local_messages(provider, thread)
        elif provider == 'muse':
            messages = muse_messages(thread['path'])
    return {'threads': threads, 'messages': messages, 'id': thread['id'] if thread else ''}


def main():
    provider = sys.argv[1]
    if provider == 'codex-monitor':
        json.dump(codex_changes(Path.home(), json.load(sys.stdin)), sys.stdout)
        return
    selected = sys.argv[2] if len(sys.argv) > 2 else ''
    listing = len(sys.argv) > 3 and sys.argv[3] == 'list'
    if provider == 'muse' and os.name == 'nt':
        # Run beside the live WAL database; UNC reads cannot safely open its WAL.
        result = subprocess.run(['wsl.exe', '-e', 'python3', '-', provider, selected,
                                 'list' if listing else 'messages'],
                                input=Path(__file__).read_text(encoding='utf-8'),
                                capture_output=True, encoding='utf-8', errors='replace', timeout=50,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError('Could not read Muse Code in the default WSL distribution. Check WSL and python3.')
        print(result.stdout)
    else:
        json.dump(snapshot(provider, selected, listing), sys.stdout)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        json.dump({'error': str(error)}, sys.stdout)
        sys.exit(1)
