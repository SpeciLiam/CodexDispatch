# CodexDispatch

Text your local Mac from your phone and have it run Codex in an allowlisted repo.

The first version supports iMessage on your Mac and Telegram long polling. Your Mac must be awake and online.

## What You Can Type

Shortest common form:

```text
codex viz
```

Run a freeform prompt in the default repo:

```text
codex check git status and summarize anything risky
```

Pick a repo alias:

```text
codex skills refresh the recruiting dashboard
codex dispatch add a README section for launchd
```

Useful commands:

Inside iMessage, start every command with `codex`:

```text
codex help
codex repos
codex s
codex t
codex w
codex x
codex a yes, submit the safe changes and keep going
```

## Stepping Away From A Long Run

Start the run from Telegram:

```text
codex skills work through the next 5 tailored applications and stop only for blockers
```

The bot will send progress pings while the run is active. If you want to check manually:

```text
codex s
codex t
```

If Codex finishes with a question or a partial result, answer from your phone:

```text
codex a use the first option and continue
```

That resumes the most recent Codex session in the same repo using `codex exec resume --last`.

If you want fewer pings:

```text
codex w off
```

CodexDispatch monitors runs it starts. If you start a separate Codex desktop/app chat directly, this bot cannot reliably read or control that unrelated chat. For away-from-keyboard work, start the run through iMessage or Telegram so the bot owns the log, status, and resume path.

## iMessage Setup

1. Make sure this Mac is signed into Messages with iMessage enabled.
2. Give your terminal Full Disk Access:
   `System Settings -> Privacy & Security -> Full Disk Access`, then enable Terminal, iTerm, or whichever app runs Python.
3. Give automation permission when macOS asks, because replies are sent through the Messages app with AppleScript.
4. Edit `config.toml` and add your phone number or Apple ID email:

```toml
imessage_allowed_senders = ["+15551234567", "you@example.com"]
```

5. Start the watcher:

```bash
cd /Users/liamvan/Documents/Repos/CodexDispatch
./imessage_dispatch.py
```

6. From your phone, text yourself or the Apple ID signed into this Mac:

```text
codex help
```

The `codex` prefix is required so normal iMessages do not dispatch local agents.

## Telegram Setup

1. In Telegram, open `@BotFather`.
2. Send `/newbot`.
3. Choose a display name and username.
4. Copy the bot token BotFather gives you.
5. On your Mac:

```bash
cd /Users/liamvan/Documents/Repos/CodexDispatch
cp config.example.toml config.toml
```

6. Put the token in `config.toml` as `telegram_bot_token`, or export it:

```bash
export TELEGRAM_BOT_TOKEN="123456:abc..."
```

7. Start the bot:

```bash
python3 codex_dispatch.py
```

8. In Telegram, message your bot:

```text
/whoami
```

9. Copy the returned chat id into `allowed_chat_ids` in `config.toml`:

```toml
allowed_chat_ids = [123456789]
```

10. Restart the bot.

## Run It

```bash
cd /Users/liamvan/Documents/Repos/CodexDispatch
python3 codex_dispatch.py
```

Keep that terminal open while you are out. For a more permanent setup, run it in `tmux`, `screen`, or a macOS LaunchAgent.

## Safety Model

- Only chats listed in `allowed_chat_ids` can run Codex.
- Repos must be listed in `[repos]`.
- Runs use `codex exec --sandbox workspace-write --ask-for-approval never` by default.
- Logs are written under `runs/<run-id>/`.

This is designed for your own trusted machine. Do not expose the bot token or add group chats unless you really mean it.
