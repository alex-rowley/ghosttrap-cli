"""ghosttrap CLI — watch for errors streaming from ghosttrap.io."""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request

import websockets

KNOWN_SKILL_HASHES = {
    "aeda67bc5971bd8af4d7ebe819ebcce5acead562fa618227a1798b4b5ae7143e",  # v0.2.0
    "0f2d2f4105e393fc69084d404d5a8154ba5d97fd23f92810c51345e3dc68e9a0",  # v0.3.0
    "8564b65b8ab5c63283cda1706e30ca62bc4e111d33ba8918220f4b556ad01da1",  # v0.3.1..v0.3.3
    "5759b2e0dc8ca47c3801915fd688cc8da878a7ab8d405f5183ffd7e8c8df4c55",  # v0.3.4..v0.3.7
    "0651bb4247cf5c68960ff5b63d6a5d0c85ff1ce08e7966ab4823601ff02cf1f4",  # v0.3.9
    "38810f43867a2a91420cc3dacbc71d2acabd7125596fd5b43f222b49725c9696",  # v0.3.10
}

__version__ = "0.3.11"

GHOSTTRAP_SERVER = "wss://ghosttrap.io/stream/"
CONFIG_DIR = os.path.expanduser("~/.ghosttrap")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
SKILL_DIR = os.path.expanduser("~/.claude/skills/ghosttrap")
SKILL_FILE = os.path.join(SKILL_DIR, "SKILL.md")
GITHUB_CLI_RELEASES = "https://api.github.com/repos/alex-rowley/ghosttrap-cli/releases/latest"
VERSION_CHECK_TTL = 86400  # check once per day


def _check_cli_version(config):
    """Check if a newer CLI version is available. Caches for 24h."""
    last_check = config.get("cli_version_check", 0)
    if time.time() - last_check < VERSION_CHECK_TTL:
        return
    try:
        req = urllib.request.Request(GITHUB_CLI_RELEASES, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ghosttrap-cli",
        })
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            latest = data.get("tag_name", "").lstrip("v")
            if latest and latest != __version__:
                print(f"ghosttrap-cli {latest} available (you have {__version__})", file=sys.stderr)
    except Exception:
        pass
    config["cli_version_check"] = time.time()
    _save_config(config)

SKILL_CONTENT = """\
---
name: ghosttrap
description: Production error monitoring via ghosttrap.io. Trigger when starting work on a configured project, when the user mentions production errors, or when you see ghosttrap references in code.
---

# Ghosttrap

Read `~/.ghosttrap/config.json` for state. It contains:
- `repos`: map keyed by GitHub repo id (stringified int) to `{"github_id": int, "owner": str, "name": str, "token": "t_xxx", "sdk_installed": bool, "sdk_version": str, "init_file": str}`.
- `cursor`: last seen error ID

## On session start

1. Detect the current repo from `git config --get remote.origin.url` (returns `owner/name`).
2. Find a matching entry in config by looking for one whose `owner`/`name` equals the detected slug. If no match, tell the user to run `ghosttrap setup`. (The owner/name on a config entry auto-refreshes from the server when the repo is renamed or transferred, so always match against the entry's stored owner/name, not the config key.)
3. If `sdk_installed` is false or missing: install the SDK (`pip install ghosttrap-sdk`), wire `ghosttrap.init("<token>")` into the app startup. For Django projects, also add `"ghosttrap.django.GhostTrapApp"` to INSTALLED_APPS (re-attaches logging handler after Django's dictConfig) and `"ghosttrap.django.GhostTrapMiddleware"` to MIDDLEWARE (catches unhandled view exceptions). The SDK auto-hooks into Celery task_failure if Celery is installed, and attaches a logging handler for logger.exception() calls. Use whatever pattern the project already uses for configuration (env vars, settings files, hardcoded — match the existing style). Then update the config: set `sdk_installed: true`, `sdk_version`, `init_file` to record what you did.
4. Run `ghosttrap peek --clear` with `run_in_background: true`. The `--clear` flag skips any stale backlog from prior sessions so you only get fresh errors.

## When peek returns

1. **Immediately restart peek** in the background before doing anything else — this ensures you're listening for the next error while you work on the current one. Use plain `ghosttrap peek` here (no `--clear`) — you only want to skip backlog at session start.
2. Read the JSON output: `error.repo`, `error.type`, `error.message`, `error.traceback` (list of strings), `error.frames` (list of `{file, line, function, code}`).
3. Open the file from the last frame, diagnose, fix.

## Other commands

- `ghosttrap last` — fetch the single most recent error and exit immediately, no waiting. Useful when the user wants to look at the latest error without starting a watch. Add `--clear` to also skip everything older in one shot.
- `ghosttrap clear` — manually skip outstanding errors without waiting. Useful if the user explicitly wants to drop the queue.

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


def _repo_key(r):
    gid = r.get("github_id")
    if gid is not None:
        return str(gid)
    return f"{r.get('owner')}/{r.get('name')}"


def _is_known_repo(config, repo_entry):
    return _repo_key(repo_entry) in config.get("repos", {})


def _save_repos(config, repos):
    if "repos" not in config:
        config["repos"] = {}
    for r in repos:
        key = _repo_key(r)
        existing = config["repos"].get(key, {})
        existing.update({
            "github_id": r.get("github_id"),
            "owner": r["owner"],
            "name": r["name"],
            "token": r["token"],
        })
        config["repos"][key] = existing
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
            if f"{r.get('owner')}/{r.get('name')}" == cwd_slug:
                return r
    return repos[0] if repos else None


def _print_setup_snippet(repo):
    owner = repo["owner"]
    name = repo["name"]
    token = repo["token"]

    print(f"\nadd to your app:\n", file=sys.stderr)
    print(f"  pip install ghosttrap-sdk\n", file=sys.stderr)
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
            print("\nghosttrap uses your GitHub identity for authentication.", file=sys.stderr)
            print("run 'gh auth login' to sign in, then try again.\n", file=sys.stderr)
            sys.exit(1)
        return token
    except FileNotFoundError:
        print("\nghosttrap requires the GitHub CLI for authentication.", file=sys.stderr)
        print("install it from https://cli.github.com then try again.\n", file=sys.stderr)
        sys.exit(1)


def _get_repo_token(config):
    """Get the repo token for the current directory from config."""
    repos = config.get("repos", {})
    cwd_repo = _detect_repo_from_cwd()
    if cwd_repo:
        for entry in repos.values():
            if f"{entry.get('owner')}/{entry.get('name')}" == cwd_repo:
                return entry["token"]
    if repos:
        return next(iter(repos.values()))["token"]
    print("error: no repos configured. run 'ghosttrap setup' first.", file=sys.stderr)
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

                new_repos = [r for r in repos if not _is_known_repo(config, r)]
                # Always sync — picks up renamed/transferred repos by github_id.
                _save_repos(config, repos)
                if new_repos:
                    target = _find_target_repo(new_repos)
                    if target:
                        _print_setup_snippet(target)

                sdk_latest = event.get("sdk_latest")
                if sdk_latest:
                    cwd_repo = _detect_repo_from_cwd()
                    if cwd_repo:
                        for entry in config.get("repos", {}).values():
                            if f"{entry.get('owner')}/{entry.get('name')}" == cwd_repo:
                                installed = entry.get("sdk_version")
                                if installed and installed != sdk_latest:
                                    print(f"ghosttrap-sdk {sdk_latest} available (you have {installed})", file=sys.stderr)
                                break

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


def _refresh_skill_if_stale():
    if not os.path.exists(SKILL_FILE):
        return
    with open(SKILL_FILE) as f:
        content = f.read()
    if content == SKILL_CONTENT:
        return
    if hashlib.sha256(content.encode()).hexdigest() in KNOWN_SKILL_HASHES:
        _write_skill()
        print("ghosttrap skill file updated", file=sys.stderr)


async def setup(server_url, token):
    config = _load_config()

    cwd_repo = _detect_repo_from_cwd()
    if not cwd_repo:
        print("error: not in a git repo, or no remote.origin.url configured", file=sys.stderr)
        sys.exit(1)

    print(f"claiming {cwd_repo}...", file=sys.stderr)

    try:
        url = f"{server_url}?token={token}&repo={cwd_repo}"
        async with websockets.connect(url) as ws:
            message = await asyncio.wait_for(ws.recv(), timeout=30)
            event = json.loads(message)

            if event.get("type") != "subscribed":
                print("error: unexpected response from server", file=sys.stderr)
                sys.exit(1)

            repos = event.get("repos", [])
            _save_repos(config, repos)
            _write_skill()

            target = repos[0] if repos else None

            print(f"claimed {cwd_repo}", file=sys.stderr)
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
        except (websockets.ConnectionClosed, websockets.InvalidStatus, ConnectionError, OSError):
            print("connection lost, reconnecting...", file=sys.stderr)
            await asyncio.sleep(60)


async def peek(server_url, token):
    config = _load_config()
    _check_cli_version(config)
    while True:
        try:
            await _connect_and_handle(server_url, token, config, once=True)
            return  # got an error, printed it, done
        except (websockets.ConnectionClosed, websockets.InvalidStatus, ConnectionError, OSError):
            print("connection lost, reconnecting...", file=sys.stderr)
            await asyncio.sleep(60)


def _advance_cursor(config, token):
    since = config.get("cursor", 0)
    server = GHOSTTRAP_SERVER.replace("wss://", "https://").replace("/stream/", "")
    url = f"{server}/latest/{token}/?since={since}"
    req = urllib.request.Request(url, headers={"User-Agent": "ghosttrap-cli"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
        latest_id = data.get("latest_id", 0)
        pending = data.get("pending", 0)
        config["cursor"] = latest_id
        _save_config(config)
        return pending


def clear():
    _require_setup()
    config = _load_config()
    token = _get_repo_token(config)
    try:
        pending = _advance_cursor(config, token)
        if pending:
            print(f"cleared {pending} error(s)", file=sys.stderr)
        else:
            print(f"nothing to clear", file=sys.stderr)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


def last(do_clear=False):
    _require_setup()
    config = _load_config()
    _check_cli_version(config)
    token = _get_repo_token(config)
    server = GHOSTTRAP_SERVER.replace("wss://", "https://").replace("/stream/", "")
    url = f"{server}/last/{token}/"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ghosttrap-cli"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    error = data.get("error")
    if not error:
        print("no errors yet", file=sys.stderr)
        return

    print(json.dumps({"type": "error", "error": error}))
    sys.stdout.flush()

    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  {error.get('repo', '?')}", file=sys.stderr)
    print(f"  {error.get('type', '?')}: {error.get('message', '')}", file=sys.stderr)
    frames = error.get("frames", [])
    if frames:
        f = frames[-1]
        print(f"  at {f.get('file', '?')}:{f.get('line', '?')} in {f.get('function', '?')}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)

    if do_clear:
        try:
            _advance_cursor(config, token)
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)


def main():
    parser = argparse.ArgumentParser(prog="ghosttrap", description="Watch for errors from ghosttrap.io")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("setup", help="Claim repos and install Claude Code skill")
    sub.add_parser("clear", help="Skip all outstanding errors")

    watch_parser = sub.add_parser("watch", help="Stream errors in real time")
    watch_parser.add_argument("--server", default=GHOSTTRAP_SERVER, help="WebSocket server URL")

    peek_parser = sub.add_parser("peek", help="Wait for the next error then exit")
    peek_parser.add_argument("--server", default=GHOSTTRAP_SERVER, help="WebSocket server URL")
    peek_parser.add_argument("--clear", action="store_true", help="Skip outstanding errors before waiting")

    last_parser = sub.add_parser("last", help="Fetch the most recent error then exit")
    last_parser.add_argument("--clear", action="store_true", help="Also skip remaining outstanding errors")

    args = parser.parse_args()

    if args.command == "setup":
        token = get_gh_token()
        asyncio.run(setup(GHOSTTRAP_SERVER, token))
    elif args.command == "clear":
        clear()
    elif args.command == "watch":
        _require_setup()
        _refresh_skill_if_stale()
        config = _load_config()
        token = _get_repo_token(config)
        asyncio.run(watch(args.server, token))
    elif args.command == "peek":
        _require_setup()
        _refresh_skill_if_stale()
        config = _load_config()
        token = _get_repo_token(config)
        if args.clear:
            try:
                _advance_cursor(config, token)
            except Exception as e:
                print(f"error: {e}", file=sys.stderr)
                sys.exit(1)
        asyncio.run(peek(args.server, token))
    elif args.command == "last":
        _refresh_skill_if_stale()
        last(do_clear=args.clear)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
