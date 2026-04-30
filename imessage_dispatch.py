#!/usr/bin/env python3
"""iMessage-to-Codex dispatcher for a local Mac.

This watches the local Messages database for incoming texts that start with a
prefix such as "codex", then routes the rest through the shared CodexDispatch
command handler.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from codex_dispatch import (
    ROOT,
    State,
    chunk_text,
    handle_message,
    load_config,
    parse_toml_subset,
)


DEFAULT_DB = Path.home() / "Library/Messages/chat.db"


class IMessageTransport:
    def send(self, chat_id: str, text: str) -> None:
        for chunk in chunk_text(text):
            send_imessage(chat_id, chunk)


def load_raw_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Missing {path}. Copy config.example.toml to config.toml first.")
    return parse_toml_subset(path.read_text())


def normalize_handle(value: str) -> str:
    return "".join(ch for ch in value if ch.isalnum() or ch == "+").lower()


def is_sender_allowed(sender: str, allowed: set[str]) -> bool:
    if not allowed:
        return True
    normalized = normalize_handle(sender)
    return normalized in {normalize_handle(item) for item in allowed}


def open_messages_db(path: Path) -> sqlite3.Connection:
    # immutable=1 makes SQLite treat the live Messages DB as read-only.
    uri = f"file:{path}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def get_latest_rowid(db_path: Path) -> int:
    try:
        with open_messages_db(db_path) as conn:
            row = conn.execute("select coalesce(max(rowid), 0) from message").fetchone()
            return int(row[0] or 0)
    except sqlite3.OperationalError as exc:
        explain_db_error(exc, db_path)
        raise


def fetch_new_messages(db_path: Path, after_rowid: int, allow_from_me: bool) -> list[tuple[int, str, str]]:
    from_me_clause = "" if allow_from_me else "and message.is_from_me = 0"
    with open_messages_db(db_path) as conn:
        rows = conn.execute(
            f"""
            select message.rowid, handle.id, message.text
            from message
            join handle on handle.rowid = message.handle_id
            where message.rowid > ?
              {from_me_clause}
              and message.text is not null
            order by message.rowid asc
            """,
            (after_rowid,),
        ).fetchall()
    return [(int(rowid), str(sender), str(text)) for rowid, sender, text in rows]


def strip_prefix(text: str, prefix: str) -> str | None:
    stripped = text.strip()
    lowered = stripped.lower()
    normalized_prefix = prefix.strip().lower()
    if lowered == normalized_prefix:
        return "help"
    if not lowered.startswith(normalized_prefix + " "):
        return None
    return stripped[len(prefix):].strip()


def send_imessage(handle: str, text: str) -> None:
    script = """
    on run argv
      set targetHandle to item 1 of argv
      set messageText to item 2 of argv
      tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy targetHandle of targetService
        send messageText to targetBuddy
      end tell
    end run
    """
    subprocess.run(["osascript", "-e", script, handle, text], check=False)


def explain_db_error(exc: sqlite3.OperationalError, db_path: Path) -> None:
    message = str(exc).lower()
    if "unable to open database" in message or "authorization" in message or "not authorized" in message:
        print(
            "\nCould not read the Messages database.\n"
            "Give Terminal or your Python launcher Full Disk Access:\n"
            "System Settings -> Privacy & Security -> Full Disk Access.\n"
            f"Database path: {db_path}\n",
            file=sys.stderr,
        )


def poll_loop(config_path: Path, replay_existing: bool = False) -> None:
    config = load_config(config_path, require_telegram=False)
    raw = load_raw_config(config_path)
    db_path = Path(raw.get("imessage_db_path", str(DEFAULT_DB))).expanduser()
    prefix = str(raw.get("imessage_prefix", "codex")).strip()
    allowed = {str(item) for item in raw.get("imessage_allowed_senders", [])}
    allow_from_me = bool(raw.get("imessage_allow_from_me", False))
    poll_seconds = int(raw.get("imessage_poll_seconds", 3))

    state = State()
    transport = IMessageTransport()
    last_rowid = 0 if replay_existing else get_latest_rowid(db_path)

    print(f"iMessage dispatch is watching for '{prefix} ...' commands.", flush=True)
    print(f"Messages DB: {db_path}", flush=True)
    if not allowed:
        print("Warning: imessage_allowed_senders is empty; any incoming sender can dispatch.", flush=True)

    while True:
        try:
            messages = fetch_new_messages(db_path, last_rowid, allow_from_me)
            for rowid, sender, text in messages:
                last_rowid = max(last_rowid, rowid)
                command = strip_prefix(text, prefix)
                if command is None:
                    continue
                if not is_sender_allowed(sender, allowed):
                    print(f"Ignoring unauthorized sender {sender}", flush=True)
                    continue
                handle_message({"chat": {"id": sender}, "text": command}, config, transport, state)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"poll error: {exc}", file=sys.stderr, flush=True)
            time.sleep(max(3, poll_seconds))
        time.sleep(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch iMessage for CodexDispatch commands.")
    parser.add_argument("--config", default=str(ROOT / "config.toml"), help="Path to config.toml")
    parser.add_argument("--replay-existing", action="store_true", help="Process existing matching messages")
    args = parser.parse_args()
    poll_loop(Path(args.config).expanduser().resolve(), replay_existing=args.replay_existing)


if __name__ == "__main__":
    main()
