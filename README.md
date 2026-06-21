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
| `ghosttrap peek --clear` | Skip outstanding errors, then wait for the next one |
| `ghosttrap last` | Fetch the most recent error and exit (no waiting) |
| `ghosttrap last --clear` | Fetch the most recent error and skip everything older |
| `ghosttrap watch` | Stream all errors continuously |
| `ghosttrap clear` | Skip all outstanding errors |
| `ghosttrap nuke` | Permanently delete every server-side row for the current repo (errors + token). Requires typed confirmation. |

`peek`, `watch`, `last`, and `clear` accept `--repo owner/name` to target a specific claimed repo when you're not inside its working tree (e.g. `ghosttrap peek --repo alex-rowley/ghosttrap-cli`). Otherwise they detect the repo from cwd. `nuke` is intentionally cwd-locked.

## How it works

- **Setup** authenticates with GitHub (via the active `gh` account) to prove you have access to the repo, then saves a repo token locally. If your active `gh` account can't see the repo, setup fails with a clear message; switch with `gh auth switch` and retry.
- **Peek** and **watch** connect to ghosttrap.io using that token — no GitHub auth needed after setup
- Errors that arrive while you're offline are replayed on next connect (cursor-based, no duplicates)
- Repos are tracked by GitHub's immutable repo id, so a rename or transfer doesn't require any action — the next connect picks up the new `owner/name` and your token keeps working
- Local state is stored in `~/.ghosttrap/config.json`, keyed by GitHub repo id

## Requirements

- Python 3.10+
- [GitHub CLI](https://cli.github.com) (`gh`) — used for authentication during setup
- [Claude Code](https://claude.ai/code) — the AI agent that fixes your errors
- macOS or Linux (Windows is untested)

## Privacy

Error data (tracebacks, exception messages, file paths) is routed through ghosttrap.io. The server is not open source yet — if there's demand for self-hosting, we'll open it up. Your GitHub token is used only during `setup` to verify repo access; it's never stored on the server. After setup, all communication uses a repo-specific token that grants access only to that repo's error stream — it cannot access your GitHub account.

User context (Django user id + username) is **never** sent unless you opt in with `ghosttrap.init(token, send_user=True)` in your app. Server hostname is captured automatically.

Run `ghosttrap nuke` from inside a repo to permanently delete every server-side row for that repo (errors + the token itself). Requires typing the repo name to confirm.
