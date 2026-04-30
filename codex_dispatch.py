#!/usr/bin/env python3
"""Shared Codex dispatch core plus a Telegram polling entrypoint."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import signal
import subprocess
import sys
import textwrap
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
MAX_TELEGRAM_MESSAGE = 3900
RUN_WORDS = {"/c", "c", "r", "/r", "/run", "run", "/do", "do", "/codex", "codex"}
STATUS_WORDS = {"/s", "s", "/status", "status", "stat", "st"}
TAIL_WORDS = {"/t", "t", "/tail", "tail", "log", "logs"}
CANCEL_WORDS = {"/x", "x", "/cancel", "cancel", "/stop", "stop", "kill"}
ANSWER_WORDS = {"/a", "a", "/answer", "answer", "/cont", "cont", "continue", "more"}
WATCH_WORDS = {"/w", "w", "/watch", "watch"}


@dataclass
class Config:
    token: str
    allowed_chat_ids: set[str]
    runs_dir: Path
    default_repo: str
    default_sandbox: str
    default_approval: str
    monitor_interval_seconds: int
    monitor_tail_lines: int
    codex_bin: str
    repos: dict[str, Path]
    shortcuts: dict[str, str]


@dataclass
class Run:
    id: str
    chat_id: str
    repo_alias: str
    repo_path: Path
    prompt: str
    started_at: str
    status: str = "queued"
    process: subprocess.Popen[str] | None = None
    log_path: Path | None = None
    final_path: Path | None = None
    returncode: int | None = None
    kind: str = "run"
    watching: bool = True
    last_monitor_size: int = 0
    monitor_thread_started: bool = False


@dataclass
class State:
    runs: dict[str, Run] = field(default_factory=dict)
    last_run_by_chat: dict[str, str] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


def load_config(path: Path, *, require_telegram: bool = True) -> Config:
    if not path.exists():
        raise SystemExit(f"Missing {path}. Copy config.example.toml to config.toml first.")

    data = parse_toml_subset(path.read_text())
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or data.get("telegram_bot_token") or ""
    if require_telegram and not token:
        raise SystemExit("Missing Telegram token. Set TELEGRAM_BOT_TOKEN or telegram_bot_token in config.toml.")

    repos = {alias: Path(value).expanduser().resolve() for alias, value in data.get("repos", {}).items()}
    if not repos:
        raise SystemExit("No repos configured. Add aliases under [repos] in config.toml.")

    default_repo = data.get("default_repo") or next(iter(repos))
    if default_repo not in repos:
        raise SystemExit(f"default_repo '{default_repo}' is not listed in [repos].")

    return Config(
        token=token,
        allowed_chat_ids={str(chat_id) for chat_id in data.get("allowed_chat_ids", [])},
        runs_dir=(ROOT / data.get("runs_dir", "runs")).resolve(),
        default_repo=default_repo,
        default_sandbox=data.get("default_sandbox", "workspace-write"),
        default_approval=data.get("default_approval", "never"),
        monitor_interval_seconds=int(data.get("monitor_interval_seconds", 90)),
        monitor_tail_lines=int(data.get("monitor_tail_lines", 18)),
        codex_bin=data.get("codex_bin", "codex"),
        repos=repos,
        shortcuts={str(key): str(value) for key, value in data.get("shortcuts", {}).items()},
    )


def parse_toml_subset(text: str) -> dict[str, Any]:
    """Parse the small TOML subset used by config.example.toml."""
    data: dict[str, Any] = {}
    current: dict[str, Any] = data

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            current = data.setdefault(section, {})
            if not isinstance(current, dict):
                raise ValueError(f"Section conflicts with scalar key: {section}")
            continue
        if "=" not in line:
            raise ValueError(f"Unsupported config line: {raw_line}")
        key, value = [part.strip() for part in line.split("=", 1)]
        current[key] = parse_value(value)
    return data


def parse_value(value: str) -> Any:
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [parse_value(part.strip()) for part in inner.split(",") if part.strip()]
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    try:
        return int(value)
    except ValueError:
        return value


class Telegram:
    def __init__(self, token: str) -> None:
        self.base = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: int = 35) -> Any:
        body = urllib.parse.urlencode(params or {}).encode()
        request = urllib.request.Request(f"{self.base}/{method}", data=body)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
        if not payload.get("ok"):
            raise RuntimeError(payload)
        return payload.get("result")

    def send(self, chat_id: str, text: str) -> None:
        for chunk in chunk_text(text):
            self.call("sendMessage", {"chat_id": chat_id, "text": chunk})


def chunk_text(text: str) -> list[str]:
    if len(text) <= MAX_TELEGRAM_MESSAGE:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        chunks.append(remaining[:MAX_TELEGRAM_MESSAGE])
        remaining = remaining[MAX_TELEGRAM_MESSAGE:]
    return chunks


def now_id() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def parse_run_request(text: str, config: Config) -> tuple[str, str] | None:
    text = text.strip()
    if not text:
        return None

    parts = text.split(maxsplit=1)
    command = parts[0].lower()
    if command in RUN_WORDS:
        rest = parts[1].strip() if len(parts) > 1 else ""
    elif command.removeprefix("@") in config.repos or command in config.shortcuts:
        rest = text
    else:
        return None

    if not rest:
        return config.default_repo, "Summarize git status and the most useful next action."

    words = rest.split(maxsplit=1)
    possible_repo = words[0].removeprefix("@").lower()
    if possible_repo in config.repos:
        repo_alias = possible_repo
        prompt = words[1].strip() if len(words) > 1 else "Summarize git status and the most useful next action."
    else:
        repo_alias = config.default_repo
        prompt = rest

    prompt = config.shortcuts.get(prompt.strip().lower(), prompt)
    return repo_alias, prompt


def parse_answer_request(text: str) -> str | None:
    text = text.strip()
    if not text:
        return None
    parts = text.split(maxsplit=1)
    if parts[0].lower() not in ANSWER_WORDS:
        return None
    return parts[1].strip() if len(parts) > 1 else "Continue from the last result."


def command_word(text: str) -> str:
    return text.strip().split(maxsplit=1)[0].lower() if text.strip() else ""


def is_allowed(chat_id: str, config: Config) -> bool:
    return not config.allowed_chat_ids or str(chat_id) in config.allowed_chat_ids


def run_codex(run: Run, config: Config, telegram: Telegram, state: State) -> None:
    run_dir = config.runs_dir / run.id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "output.log"
    final_path = run_dir / "final.txt"
    meta_path = run_dir / "meta.json"

    run.log_path = log_path
    run.final_path = final_path
    run.status = "running"
    meta_path.write_text(json.dumps({
        "id": run.id,
        "chat_id": run.chat_id,
        "repo_alias": run.repo_alias,
        "repo_path": str(run.repo_path),
        "prompt": run.prompt,
        "started_at": run.started_at,
        "kind": run.kind,
    }, indent=2))

    if run.kind == "resume":
        cmd = [
            config.codex_bin,
            "exec",
            "resume",
            "--last",
            "-o",
            str(final_path),
            run.prompt,
        ]
    else:
        cmd = [
            config.codex_bin,
            "exec",
            "--cd",
            str(run.repo_path),
            "--sandbox",
            config.default_sandbox,
            "--ask-for-approval",
            config.default_approval,
            "-o",
            str(final_path),
            run.prompt,
        ]

    with log_path.open("w") as log:
        log.write("$ " + " ".join(shlex.quote(part) for part in cmd) + "\n\n")
        log.flush()
        process = subprocess.Popen(
            cmd,
            cwd=run.repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        with state.lock:
            run.process = process
        start_monitor(run, config, telegram)
        assert process.stdout is not None
        for line in process.stdout:
            log.write(line)
            log.flush()
        run.returncode = process.wait()

    run.status = "done" if run.returncode == 0 else "failed"
    final = final_path.read_text(errors="replace").strip() if final_path.exists() else ""
    if not final:
        final = tail_file(log_path, 80) or "(No output captured.)"
    telegram.send(run.chat_id, f"Run {run.id} {run.status} in {run.repo_alias}.\n\n{final}")


def start_monitor(run: Run, config: Config, telegram: Telegram) -> None:
    if run.monitor_thread_started:
        return
    run.monitor_thread_started = True
    worker = threading.Thread(target=monitor_run, args=(run, config, telegram), daemon=True)
    worker.start()


def monitor_run(run: Run, config: Config, telegram: Telegram) -> None:
    while run.status == "running":
        time.sleep(max(15, config.monitor_interval_seconds))
        if run.status != "running" or not run.watching or not run.log_path or not run.log_path.exists():
            continue
        size = run.log_path.stat().st_size
        if size <= run.last_monitor_size:
            continue
        run.last_monitor_size = size
        tail = tail_file(run.log_path, config.monitor_tail_lines).strip()
        if tail:
            telegram.send(run.chat_id, f"Still running {run.id} in {run.repo_alias}.\n\n{tail}")


def tail_file(path: Path | None, lines: int = 40) -> str:
    if not path or not path.exists():
        return ""
    data = path.read_text(errors="replace").splitlines()
    return "\n".join(data[-lines:])


def start_run(
    chat_id: str,
    repo_alias: str,
    prompt: str,
    config: Config,
    telegram: Telegram,
    state: State,
    *,
    kind: str = "run",
) -> None:
    run = Run(
        id=now_id(),
        chat_id=chat_id,
        repo_alias=repo_alias,
        repo_path=config.repos[repo_alias],
        prompt=prompt,
        started_at=dt.datetime.now().isoformat(timespec="seconds"),
        kind=kind,
    )
    with state.lock:
        state.runs[run.id] = run
        state.last_run_by_chat[chat_id] = run.id

    label = "Continuing" if kind == "resume" else "Started"
    telegram.send(chat_id, f"{label} {run.id} in {repo_alias}.\n\n{prompt}")
    worker = threading.Thread(target=run_codex, args=(run, config, telegram, state), daemon=True)
    worker.start()


def last_run(chat_id: str, state: State) -> Run | None:
    with state.lock:
        run_id = state.last_run_by_chat.get(chat_id)
        return state.runs.get(run_id) if run_id else None


def handle_message(message: dict[str, Any], config: Config, telegram: Telegram, state: State) -> None:
    chat_id = str(message["chat"]["id"])
    text = str(message.get("text", "")).strip()

    if not is_allowed(chat_id, config):
        telegram.send(chat_id, f"Not allowed. Your chat id is {chat_id}.")
        return

    word = command_word(text)
    if word in {"/start", "/help", "help"}:
        telegram.send(chat_id, help_text(config))
        return
    if word == "/whoami":
        telegram.send(chat_id, f"chat_id: {chat_id}")
        return
    if word in {"/repos", "repos"}:
        repos = "\n".join(f"{alias}: {path}" for alias, path in config.repos.items())
        telegram.send(chat_id, repos)
        return
    if word in STATUS_WORDS:
        run = last_run(chat_id, state)
        telegram.send(chat_id, format_status(run) if run else "No runs yet.")
        return
    if word in TAIL_WORDS:
        run = last_run(chat_id, state)
        telegram.send(chat_id, tail_file(run.log_path) if run else "No runs yet.")
        return
    if word in WATCH_WORDS:
        run = last_run(chat_id, state)
        telegram.send(chat_id, toggle_watch(run, text) if run else "No runs yet.")
        return
    if word in CANCEL_WORDS:
        run = last_run(chat_id, state)
        telegram.send(chat_id, cancel_run(run) if run else "No runs yet.")
        return

    answer = parse_answer_request(text)
    if answer:
        run = last_run(chat_id, state)
        if not run:
            telegram.send(chat_id, "No previous run to continue.")
            return
        if run.status == "running":
            telegram.send(chat_id, f"{run.id} is still running. Use t for logs or x to cancel it first.")
            return
        start_run(chat_id, run.repo_alias, answer, config, telegram, state, kind="resume")
        return

    parsed = parse_run_request(text, config)
    if parsed:
        repo_alias, prompt = parsed
        start_run(chat_id, repo_alias, prompt, config, telegram, state)
        return

    telegram.send(chat_id, "I did not understand that. Try viz, skills git status, a keep going, s, t, or /help.")


def format_status(run: Run | None) -> str:
    if not run:
        return "No runs yet."
    return textwrap.dedent(f"""\
        {run.id}: {run.status}
        repo: {run.repo_alias}
        started: {run.started_at}
        returncode: {run.returncode}
        prompt: {run.prompt}
    """).strip()


def cancel_run(run: Run | None) -> str:
    if not run or not run.process or run.process.poll() is not None:
        return "No active run to cancel."
    run.process.send_signal(signal.SIGINT)
    run.status = "cancelled"
    return f"Sent cancel to {run.id}."


def toggle_watch(run: Run | None, text: str) -> str:
    if not run:
        return "No runs yet."
    lowered = text.lower()
    if "off" in lowered:
        run.watching = False
    elif "on" in lowered:
        run.watching = True
    else:
        run.watching = not run.watching
    state = "on" if run.watching else "off"
    return f"Watch is {state} for {run.id}."


def help_text(config: Config) -> str:
    shortcuts = ", ".join(sorted(config.shortcuts)) or "(none)"
    repos = ", ".join(sorted(config.repos))
    return textwrap.dedent(f"""\
        CodexDispatch commands:

        /c <prompt>              run in default repo ({config.default_repo})
        /c <repo> <prompt>       run in a repo alias
        /s                       last run status
        /t                       tail last run log
        /w                       toggle auto progress pings
        /x                       cancel last active run
        /a <prompt>              continue/answer the last finished Codex session
        /repos                   list repo aliases
        /whoami                  show your Telegram chat id

        Bare shortcuts work too: viz
        Bare repo prompts work too: skills git status
        Short aliases: c, r, run, do, a, s, t, w, x
        Repos: {repos}
        Shortcuts: {shortcuts}

        Examples:
        viz
        c skills check git status
        a yes, use the first option and keep going
        do dispatch improve the README
    """).strip()


def poll_loop(config: Config) -> None:
    telegram = Telegram(config.token)
    state = State()
    config.runs_dir.mkdir(parents=True, exist_ok=True)
    offset = 0
    print("CodexDispatch is running. Press Ctrl-C to stop.", flush=True)

    while True:
        try:
            updates = telegram.call("getUpdates", {"offset": offset, "timeout": 25}, timeout=35)
            for update in updates:
                offset = max(offset, int(update["update_id"]) + 1)
                message = update.get("message")
                if message and "text" in message:
                    handle_message(message, config, telegram, state)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"poll error: {exc}", file=sys.stderr, flush=True)
            time.sleep(3)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Telegram bot that dispatches local Codex jobs.")
    parser.add_argument("--config", default=str(ROOT / "config.toml"), help="Path to config.toml")
    args = parser.parse_args()
    poll_loop(load_config(Path(args.config).expanduser().resolve()))


if __name__ == "__main__":
    main()
