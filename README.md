# ghosttrap-cli

The developer-side listener for [ghosttrap](https://ghosttrap.io). Connects errors from remote servers to Claude Code in real time.

Works with any Python app. Django and Celery get deep integration (middleware, app config, task failure hooks). Flask and other frameworks work via Python's logging and excepthook.

## Setup

Requires the [GitHub CLI](https://cli.github.com) (`gh`) and [Claude Code](https://claude.ai/code).

```
pip install ghosttrap-cli
cd ~/your-project
ghosttrap setup
```

Then in Claude Code:

```
/ghosttrap
```

That's it. `setup` authenticates via `gh`, claims the repo, and installs a Claude Code skill. The `/ghosttrap` skill handles everything else — it installs the SDK into your app, wires in the error hooks, and starts monitoring.

## What happens next

The skill file tells Claude Code to run `ghosttrap peek` in the background. Peek opens a WebSocket to ghosttrap.io and waits. When a production error arrives, Claude sees the full traceback — exception type, message, file, line, function — and starts fixing.

After fixing, Claude restarts peek and waits for the next one. Errors become a real-time stream that your AI agent dispatches automatically.

## The SDK

Your app needs [ghosttrap-sdk](https://github.com/alex-rowley/ghosttrap-sdk) to report errors. The Claude Code skill handles the integration automatically — it installs the SDK, wires it into your app, and adds Django/Celery hooks if applicable. You shouldn't need to touch the SDK manually.

## Commands

| Command | What it does |
|---------|-------------|
| `ghosttrap setup` | Claim a repo, install the Claude Code skill |
| `ghosttrap peek` | Wait for the next error, print it, exit |
| `ghosttrap watch` | Stream all errors continuously |

## How it works

- **Setup** authenticates with GitHub to prove you own the repo, then saves a token locally
- **Peek** and **watch** connect to ghosttrap.io using that token — no GitHub auth needed after setup
- Errors that arrive while you're offline are replayed on next connect (cursor-based, no duplicates)
- Local state is stored in `~/.ghosttrap/config.json`

## Requirements

- Python 3.10+
- [GitHub CLI](https://cli.github.com) (`gh`) — used for authentication during setup
- [Claude Code](https://claude.ai/code) — the AI agent that fixes your errors
- macOS or Linux (Windows is untested)

## Privacy

Error data (tracebacks, exception messages, file paths) is routed through ghosttrap.io. The server is not open source yet — if there's demand for self-hosting, we'll open it up. Your GitHub token is used only during `setup` to verify repo ownership; it's never stored on the server. After setup, all communication uses a repo-specific token that grants access only to that repo's error stream — it cannot access your GitHub account.
