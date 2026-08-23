# Copyright (C) 2026 Will Bishop
# This file is covered by the GNU General Public License.

"""Read ChatGPT Codex task rows for an NVDA process without sqlite3."""

import json
import sqlite3
import sys


def _column_or_null(columns, name):
	return name if name in columns else "NULL"


def load_tasks(path):
	connection = sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro", uri=True, timeout=0.25)
	try:
		columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
		if not {"id", "archived"}.issubset(columns):
			raise RuntimeError("threads table has no supported schema")
		updated_column = next(
			(name for name in (
				"recency_at_ms", "updated_at_ms", "recency_at", "updated_at", "created_at_ms", "created_at",
			) if name in columns),
			None,
		)
		where = ["archived = 0"]
		if "thread_source" in columns:
			where.append("(thread_source IS NULL OR thread_source != 'subagent')")
		query = "SELECT %s FROM threads WHERE %s" % (
			", ".join((
				"id",
				_column_or_null(columns, "rollout_path"),
				_column_or_null(columns, "name"),
				_column_or_null(columns, "title"),
				_column_or_null(columns, "preview"),
				_column_or_null(columns, "first_user_message"),
				_column_or_null(columns, "cwd"),
				updated_column or "0",
			)),
			" AND ".join(where),
		)
		if updated_column:
			query += " ORDER BY %s DESC" % updated_column
		return [
			{
				"id": task_id,
				"rolloutPath": rollout_path,
				"name": name,
				"title": title,
				"preview": preview,
				"firstUserMessage": first_message,
				"cwd": cwd,
				"updatedAt": updated_at,
			}
			for task_id, rollout_path, name, title, preview, first_message, cwd, updated_at in connection.execute(query)
		]
	finally:
		connection.close()


def main():
	if len(sys.argv) != 2:
		raise SystemExit("usage: chatgptDb.py STATE_DATABASE")
	# ASCII escapes keep output portable when a Windows Python process inherits
	# a legacy console code page. The backend's JSON decoder restores Unicode.
	json.dump({"tasks": load_tasks(sys.argv[1])}, sys.stdout)


if __name__ == "__main__":
	main()
