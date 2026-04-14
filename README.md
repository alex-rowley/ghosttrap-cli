# ghosttrap-cli

The developer-side listener for [ghosttrap](https://ghosttrap.io). Connects production errors to Claude Code in real time.

## Setup

```
pip install ghosttrap-cli
```

Then, from inside your project directory:

```
ghosttrap setup
```

This authenticates via your local [GitHub CLI](https://cli.github.com) (`gh`), claims the repo on ghosttrap.io, and installs a Claude Code skill file that teaches Claude how to monitor for errors and fix them.

That's the entire setup. Two commands. Claude Code takes it from here.

## What happens next

The skill file tells Claude Code to run `ghosttrap peek` in the background. Peek opens a WebSocket to ghosttrap.io and waits. When a production error arrives, Claude sees the full traceback — exception type, message, file, line, function — and starts fixing.

After fixing, Claude restarts peek and waits for the next one. Errors become a real-time stream that your AI agent dispatches automatically.

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

## Links

- [ghosttrap-sdk](https://github.com/arowley-predictive-power/ghosttrap-sdk) — the SDK you drop into your Python app to report errors
- [ghosttrap.io](https://ghosttrap.io) — the server that routes errors from your app to your agent
