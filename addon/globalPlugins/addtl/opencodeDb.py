#!/usr/bin/env python
"""Helper to query OpenCode's SQLite database for the NVDA addon.
Called via subprocess.  Returns JSON with latest session and its messages.
Usage: python opencodeDb.py <db_path> [--list]

NOTE FOR agentDesktopAccessibility 2.0.0:
This file lives in globalPlugins/ so opencodeBackend.py can find it via
os.path.dirname(__file__). NVDA's plugin loader will also try to import
this file as a plugin at startup, which fails because NVDA's embedded
Python doesn't have sqlite3. We:
  1. Wrap the sqlite3 import in a try/except so the import doesn't raise
  2. Provide a no-op GlobalPlugin class so the plugin loader's search
     for `GlobalPlugin` doesn't log an AttributeError
  3. The real execution is always via subprocess (system Python, which
     has sqlite3), so the no-op fallback never runs in practice.
"""
try:
    import sqlite3
    _SQLITE3_AVAILABLE = True
except ImportError:
    # NVDA's embedded Python lacks sqlite3. The plugin loader's import
    # of this file will hit this branch; that's fine because
    # opencodeBackend only ever calls us via subprocess.run() with the
    # system Python.
    _SQLITE3_AVAILABLE = False
    sqlite3 = None  # type: ignore
import json
import sys
import os


def write_json(obj):
    """Write JSON to stdout safely regardless of console encoding."""
    text = json.dumps(obj, ensure_ascii=False)
    sys.stdout.buffer.write(text.encode('utf-8'))
    sys.stdout.buffer.write(b'\n')
    sys.stdout.buffer.flush()


def list_sessions(conn):
    rows = conn.execute(
        "SELECT id, title, directory FROM session "
        "WHERE time_archived IS NULL AND parent_id IS NULL "
        "ORDER BY time_updated DESC LIMIT 100"
    ).fetchall()
    sessions = []
    for r in rows:
        sid = r["id"]
        title = (r["title"] or "").strip()
        directory = (r["directory"] or "").strip()
        sessions.append({
            "id": sid,
            "title": title,
            "directory": directory,
        })
    write_json({"sessions": sessions})


def latest_session(conn):
    session_row = conn.execute(
        "SELECT id, directory FROM session ORDER BY time_updated DESC LIMIT 1"
    ).fetchone()
    if not session_row:
        write_json({})
        return

    sid = session_row["id"]
    directory = session_row["directory"] or ""

    msg_rows = conn.execute(
        "SELECT id, json_extract(data, '$.role') as role FROM message "
        "WHERE session_id=? ORDER BY time_created",
        (sid,)
    ).fetchall()

    messages = []
    if msg_rows:
        msg_ids = [r["id"] for r in msg_rows]
        placeholders = ",".join("?" for _ in msg_ids)
        part_rows = conn.execute(
            f"SELECT message_id, json_extract(data, '$.type') as ptype, "
            f"json_extract(data, '$.text') as text "
            f"FROM part WHERE message_id IN ({placeholders}) "
            f"ORDER BY time_created",
            msg_ids
        ).fetchall()

        parts_by_msg = {}
        for p in part_rows:
            mid = p["message_id"]
            if mid not in parts_by_msg:
                parts_by_msg[mid] = []
            parts_by_msg[mid].append(p)

        for msg_row in msg_rows:
            mid = msg_row["id"]
            role_raw = (msg_row["role"] or "").lower()
            parts = parts_by_msg.get(mid, [])

            texts = []
            thoughts = []
            for p in parts:
                ptype = p["ptype"]
                txt = (p["text"] or "").strip()
                if not txt:
                    continue
                if ptype == "text":
                    texts.append(txt)
                elif ptype == "reasoning":
                    thoughts.append(txt)

            text = "\n".join(texts).strip()
            thinking = "\n".join(thoughts).strip()

            if not text and not thinking:
                continue

            role = "You" if role_raw == "user" else "Assistant"
            messages.append({
                "role": role,
                "text": text,
                "thinking": thinking,
            })

    write_json({
        "session_id": sid,
        "session_dir": directory,
        "messages": messages,
    })


def main():
    if not _SQLITE3_AVAILABLE:
        # Should never be called this way — opencodeBackend always invokes
        # us via subprocess.run([system_python, ...]). If we somehow get
        # here, exit silently.
        sys.exit(0)
    if len(sys.argv) < 2:
        sys.exit(1)
    db_path = sys.argv[1]
    list_mode = len(sys.argv) > 2 and sys.argv[2] == "--list"

    if not os.path.isfile(db_path):
        write_json({} if not list_mode else {"sessions": []})
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        if list_mode:
            list_sessions(conn)
        else:
            latest_session(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
