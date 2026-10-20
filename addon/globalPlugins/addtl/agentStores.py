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








def proto_fields(data):
    """Decode wire fields; reject truncated or unsupported data without guessing text."""
    offset = 0
    def varint():
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            if offset >= len(data):
                raise ValueError('Truncated Antigravity field')
            byte = data[offset]
            offset += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError('Invalid Antigravity varint')
    while offset < len(data):
        tag = varint()
        wire = tag & 7
        if not tag >> 3:
            raise ValueError('Invalid Antigravity field')
        if wire == 0:
            value = varint()
        elif wire in (1, 2, 5):
            length = varint() if wire == 2 else (8 if wire == 1 else 4)
            end = offset + length
            if end > len(data):
                raise ValueError('Truncated Antigravity payload')
            value, offset = data[offset:end], end
        else:
            raise ValueError('Unsupported Antigravity wire type')
        yield tag >> 3, value


def agy_messages(path):
    messages = []
    with closing(connect(path)) as db:
        for kind, payload in db.execute('SELECT step_type, step_payload FROM steps '
                                        'WHERE step_type IN (5,15) ORDER BY idx'):
            # Installed Antigravity schema: userInput=10, modelResponse=20.
            for field, value in proto_fields(payload or b''):
                if field != (10 if kind == 5 else 20) or not isinstance(value, bytes):
                    continue
                parts = dict(proto_fields(value))
                text = parts.get(26 if kind == 5 else 1, b'')
                thought = parts.get(3, b'') if kind == 15 else b''
                if isinstance(text, bytes) and isinstance(thought, bytes) and (text or thought):
                    messages.append(message('user' if kind == 5 else 'assistant',
                                            text.decode('utf-8'), thought.decode('utf-8')))
    return messages





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
    elif provider == 'agy':
        paths = sorted((Path.home() / '.gemini/antigravity/conversations').glob('*.db'),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        threads = [{'id': p.stem, 'title': p.stem, 'detail': 'Antigravity', 'path': str(p)} for p in paths]
    else:
        raise ValueError('Unknown transcript source')
    thread = select_thread(threads, selected)
    messages = []
    if thread and not listing:
        if provider in ('codex',):
            messages = local_messages(provider, thread)
        elif provider == 'agy':
            messages = agy_messages(thread['path'])
    return {'threads': threads, 'messages': messages, 'id': thread['id'] if thread else ''}


def main():
    provider = sys.argv[1]
    if provider == 'codex-monitor':
        json.dump(codex_changes(Path.home(), json.load(sys.stdin)), sys.stdout)
        return
    selected = sys.argv[2] if len(sys.argv) > 2 else ''
    listing = len(sys.argv) > 3 and sys.argv[3] == 'list'
    json.dump(snapshot(provider, selected, listing), sys.stdout)



if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        json.dump({'error': str(error)}, sys.stdout)
        sys.exit(1)
