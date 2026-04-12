"""ghosttrap CLI — watch for errors streaming from ghosttrap.io."""

import argparse
import asyncio
import json
import os
import subprocess
import sys

import websockets


GHOSTTRAP_SERVER = "wss://ghosttrap.io/stream/"
CONFIG_DIR = os.path.expanduser("~/.ghosttrap")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
SKILL_DIR = os.path.expanduser("~/.claude/skills/ghosttrap")
SKILL_FILE = os.path.join(SKILL_DIR, "SKILL.md")

SKILL_CONTENT = """\
---
name: ghosttrap
description: Production error monitoring via ghosttrap.io. Trigger when starting work on a configured project, when the user mentions production errors, or when you see ghosttrap references in code.
---

# Ghosttrap

Read `~/.ghosttrap/config.json` for state. It contains:
- `repos`: map of `"owner/repo"` to `{"token": "t_xxx", "sdk_installed": bool, "sdk_version": str, "init_file": str}`
- `cursor`: last seen error ID

## On session start

1. Detect the current repo from `git config --get remote.origin.url`.
2. Look it up in the config. If the repo isn't there, tell the user to run `ghosttrap setup`.
3. If `sdk_installed` is false or missing: install the SDK (`pip install git+https://github.com/arowley-predictive-power/ghosttrap-sdk.git`), add `import ghosttrap; ghosttrap.init("<token>")` to the app startup, add `ghosttrap.report(exc)` in exception handlers. Then update the config: set `sdk_installed: true`, `sdk_version`, `init_file` to record what you did.
4. Run `ghosttrap peek` with `run_in_background: true`.

## When peek returns

The output is JSON with `error.repo`, `error.type`, `error.message`, `error.traceback` (list of strings), `error.frames` (list of `{file, line, function, code}`). Open the file from the last frame, diagnose, fix. Then run `ghosttrap peek` in the background again.

## Rules

- Always `run_in_background: true` for peek — it blocks.
- Don't run multiple peeks at once.
- If peek exits without output (connection lost), restart it.
- After installing/updating the SDK, write the state back to config.json.
"""


def _load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {"repos": {}}


def _save_config(config):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def _is_known_repo(config, owner, name):
    return f"{owner}/{name}" in config.get("repos", {})


def _save_repos(config, repos):
    if "repos" not in config:
        config["repos"] = {}
    for r in repos:
        key = f"{r['owner']}/{r['name']}"
        config["repos"][key] = {"token": r["token"]}
    _save_config(config)


def _detect_repo_from_cwd():
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=5,
        )
        url = result.stdout.strip()
        if not url:
            return None
        for prefix in ["git@github.com:", "https://github.com/"]:
            if url.startswith(prefix):
                path = url[len(prefix):]
                if path.endswith(".git"):
                    path = path[:-4]
                return path
        if ":" in url and not url.startswith("http"):
            path = url.split(":", 1)[1]
            if path.endswith(".git"):
                path = path[:-4]
            return path
    except Exception:
        pass
    return None


def _find_target_repo(repos):
    cwd_slug = _detect_repo_from_cwd()
    if cwd_slug:
        for r in repos:
            if f"{r['owner']}/{r['name']}" == cwd_slug:
                return r
    return repos[0] if repos else None


def _print_setup_snippet(repo):
    owner = repo["owner"]
    name = repo["name"]
    token = repo["token"]

    print(f"\nadd to your app:\n", file=sys.stderr)
    print(f"  pip install git+https://github.com/arowley-predictive-power/ghosttrap-sdk.git\n", file=sys.stderr)
    print(f"  import ghosttrap\n", file=sys.stderr)
    print(f"  # option 1: token (recommended)", file=sys.stderr)
    print(f'  ghosttrap.init("{token}")\n', file=sys.stderr)
    print(f"  # option 2: repo url", file=sys.stderr)
    print(f'  ghosttrap.init("https://ghosttrap.io/trap/{owner}/{name}/")\n', file=sys.stderr)


def get_gh_token():
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True, text=True, timeout=10,
        )
        token = result.stdout.strip()
        if result.returncode != 0 or not token:
            print("error: could not get gh auth token. run 'gh auth login' first.", file=sys.stderr)
            sys.exit(1)
        return token
    except FileNotFoundError:
        print("error: gh cli not found. install it from https://cli.github.com", file=sys.stderr)
        sys.exit(1)


async def _connect_and_handle(server_url, token, config, once=False):
    """Core WebSocket loop. If once=True, exit after the first error."""
    since = config.get("cursor")
    url = f"{server_url}?token={token}"
    if since is not None:
        url += f"&since={since}"

    async with websockets.connect(url) as ws:
        async for message in ws:
            event = json.loads(message)

            if event.get("type") == "subscribed":
                repos = event.get("repos", [])
                print(f"watching {len(repos)} repo(s)", file=sys.stderr)

                new_repos = [r for r in repos if not _is_known_repo(config, r["owner"], r["name"])]
                if new_repos:
                    _save_repos(config, repos)
                    target = _find_target_repo(new_repos)
                    if target:
                        _print_setup_snippet(target)

                if not once:
                    print(f"waiting for errors...", file=sys.stderr)
                continue

            if event.get("type") == "error":
                error_id = event.get("error", {}).get("id")
                if error_id is not None:
                    config["cursor"] = error_id
                    _save_config(config)

                print(json.dumps(event))
                sys.stdout.flush()

                if not once:
                    error = event["error"]
                    print(f"\n{'='*60}", file=sys.stderr)
                    print(f"  {error.get('repo', '?')}", file=sys.stderr)
                    print(f"  {error.get('type', '?')}: {error.get('message', '')}", file=sys.stderr)
                    frames = error.get("frames", [])
                    if frames:
                        f = frames[-1]
                        print(f"  at {f.get('file', '?')}:{f.get('line', '?')} in {f.get('function', '?')}", file=sys.stderr)
                    print(f"{'='*60}", file=sys.stderr)

                if once:
                    return


def _require_setup():
    if not os.path.exists(CONFIG_FILE):
        print("error: ghosttrap is not set up. run 'ghosttrap setup' first.", file=sys.stderr)
        sys.exit(1)


def _write_skill():
    os.makedirs(SKILL_DIR, exist_ok=True)
    with open(SKILL_FILE, "w") as f:
        f.write(SKILL_CONTENT)


async def setup(server_url, token):
    config = _load_config()
    print("connecting to ghosttrap.io...", file=sys.stderr)

    try:
        url = f"{server_url}?token={token}"
        async with websockets.connect(url) as ws:
            message = await asyncio.wait_for(ws.recv(), timeout=30)
            event = json.loads(message)

            if event.get("type") != "subscribed":
                print("error: unexpected response from server", file=sys.stderr)
                sys.exit(1)

            repos = event.get("repos", [])
            _save_repos(config, repos)
            _write_skill()

            target = _find_target_repo(repos)

            print(f"\nclaimed {len(repos)} repo(s)", file=sys.stderr)
            print(f"skill file written to {SKILL_FILE}", file=sys.stderr)

            if target:
                _print_setup_snippet(target)

            print("done — Claude Code will take it from here\n", file=sys.stderr)

    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


async def watch(server_url, token):
    config = _load_config()
    print(f"connecting to {server_url}...", file=sys.stderr)

    while True:
        try:
            await _connect_and_handle(server_url, token, config, once=False)
        except websockets.ConnectionClosed:
            print("connection lost, reconnecting...", file=sys.stderr)
            await asyncio.sleep(1)


async def peek(server_url, token):
    config = _load_config()
    await _connect_and_handle(server_url, token, config, once=True)


def main():
    parser = argparse.ArgumentParser(prog="ghosttrap", description="Watch for errors from ghosttrap.io")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("setup", help="Claim repos and install Claude Code skill")

    watch_parser = sub.add_parser("watch", help="Stream errors in real time")
    watch_parser.add_argument("--server", default=GHOSTTRAP_SERVER, help="WebSocket server URL")

    peek_parser = sub.add_parser("peek", help="Wait for the next error then exit")
    peek_parser.add_argument("--server", default=GHOSTTRAP_SERVER, help="WebSocket server URL")

    args = parser.parse_args()

    if args.command == "setup":
        token = get_gh_token()
        asyncio.run(setup(GHOSTTRAP_SERVER, token))
    elif args.command == "watch":
        _require_setup()
        token = get_gh_token()
        asyncio.run(watch(args.server, token))
    elif args.command == "peek":
        _require_setup()
        token = get_gh_token()
        asyncio.run(peek(args.server, token))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
