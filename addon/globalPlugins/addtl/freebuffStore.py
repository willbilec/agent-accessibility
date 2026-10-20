#!/usr/bin/env python
# globalPlugins/addtl/freebuffStore.py
#
# Reads Freebuff Desktop's local store for the NVDA add-on.
#
# Freebuff keeps one SQLite database per project:
#   ~/.config/freebuff-desktop/projects/<name>-<projectId>/desktop-v2.db
# with a `threads` table and a `messages` table (parts_json per message), and
# a single state.json naming the active thread:
#   ~/.config/freebuff-desktop/state.json  ->  workspace.activeId
#
# NVDA's embedded Python has no sqlite3, so freebuffBackend.py runs this file
# with the system Python via subprocess and reads the JSON it prints.
#
# Usage:
#   python freebuffStore.py <config_dir>            # active thread messages
#   python freebuffStore.py <config_dir> --list     # every thread, newest first

try:
	import sqlite3
	_SQLITE3_AVAILABLE = True
except ImportError:
	# NVDA's embedded Python lacks sqlite3; the plugin loader imports this
	# file at startup and this branch keeps that import quiet. The real work
	# always runs under the system Python.
	_SQLITE3_AVAILABLE = False
	sqlite3 = None  # type: ignore

import glob
import json
import os
import sys

try:
	import globalPluginHandler

	class GlobalPlugin(globalPluginHandler.GlobalPlugin):
		"""No-op stub so NVDA's plugin loader does not log an AttributeError."""
		scriptCategory = "Agent Desktop Accessibility (helpers — should never see this category)"

except ImportError:
	pass


def write_json(payload):
	text = json.dumps(payload, ensure_ascii=False)
	sys.stdout.buffer.write(text.encode("utf-8"))
	sys.stdout.buffer.write(b"\n")
	sys.stdout.buffer.flush()


def _column_or_null(columns, name):
	return name if name in columns else "NULL"


def _connect(path):
	try:
		return sqlite3.connect(
			"file:" + path.replace("\\", "/") + "?mode=ro", uri=True, timeout=0.25
		)
	except Exception:
		return sqlite3.connect(path, timeout=0.25)


def _project_databases(config_dir):
	return sorted(glob.glob(os.path.join(config_dir, "projects", "*", "desktop-v2.db")))


def _thread_rows(connection):
	columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
	if "id" not in columns:
		return []
	query = "SELECT %s FROM threads" % ", ".join((
		"id",
		_column_or_null(columns, "title"),
		_column_or_null(columns, "project_path"),
		_column_or_null(columns, "updated_at"),
		_column_or_null(columns, "archived_at"),
		_column_or_null(columns, "status"),
	))
	return list(connection.execute(query))


def _parts_to_message(parts_json):
	texts = []
	thoughts = []
	try:
		parts = json.loads(parts_json or "[]")
	except ValueError:
		return "", ""
	for part in parts:
		if not isinstance(part, dict):
			continue
		kind = part.get("kind")
		if kind in ("text", "notice"):
			value = (part.get("text") or "").strip()
			if value:
				texts.append(value)
		elif kind in ("reasoning", "thinking"):
			value = (part.get("text") or "").strip()
			if value:
				thoughts.append(value)
	return "\n".join(texts).strip(), "\n".join(thoughts).strip()


def _state(config_dir):
	path = os.path.join(config_dir, "state.json")
	try:
		with open(path, encoding="utf-8") as handle:
			return json.load(handle)
	except Exception:
		return {}


def _active_thread_id(state):
	workspace = state.get("workspace") or {}
	return workspace.get("activeId") or ""


def list_threads(config_dir):
	threads = []
	for db_path in _project_databases(config_dir):
		connection = None
		try:
			connection = _connect(db_path)
			for row in _thread_rows(connection):
				archived = row[4]
				if archived:
					continue
				threads.append({
					"id": row[0],
					"title": (row[1] or "").strip(),
					"projectPath": row[2] or "",
					"updatedAt": row[3] or 0,
					"status": row[5] or "",
				})
		except Exception:
			continue
		finally:
			if connection is not None:
				connection.close()
	threads.sort(key=lambda thread: thread["updatedAt"] or 0, reverse=True)
	write_json({"threads": threads})


def active_thread(config_dir):
	state = _state(config_dir)
	thread_id = _active_thread_id(state)
	if not thread_id:
		write_json({})
		return
	for db_path in _project_databases(config_dir):
		connection = None
		try:
			connection = _connect(db_path)
			columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
			if "thread_id" not in columns:
				continue
			row = connection.execute(
				"SELECT title, project_path FROM threads WHERE id = ?", (thread_id,)
			).fetchone()
			if row is None:
				continue
			messages = []
			for role, parts_json in connection.execute(
				"SELECT role, parts_json FROM messages WHERE thread_id = ? ORDER BY seq",
				(thread_id,),
			):
				text, thinking = _parts_to_message(parts_json)
				if not (text or thinking):
					continue
				messages.append({
					"role": "You" if (role or "").lower() == "user" else "Assistant",
					"text": text,
					"thinking": thinking,
				})
			write_json({
				"thread_id": thread_id,
				"title": (row[0] or "").strip(),
				"project_path": row[1] or "",
				"messages": messages,
			})
			return
		except Exception:
			continue
		finally:
			if connection is not None:
				connection.close()
	write_json({"thread_id": thread_id, "messages": []})


def main():
	if not _SQLITE3_AVAILABLE:
		# Reached only if NVDA's own Python runs this file by accident.
		sys.exit(0)
	if len(sys.argv) < 2:
		sys.exit(1)
	config_dir = sys.argv[1]
	if not os.path.isdir(config_dir):
		write_json({"threads": []} if "--list" in sys.argv else {})
		return
	if "--list" in sys.argv:
		list_threads(config_dir)
	else:
		active_thread(config_dir)


if __name__ == "__main__":
	main()
