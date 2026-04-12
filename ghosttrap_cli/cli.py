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
    url = f"{server_url}?token={token}"

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


async def watch(server_url, token):
    config = _load_config()
    print(f"connecting to {server_url}...", file=sys.stderr)

    while True:
        try:
            await _connect_and_handle(server_url, token, config, once=False)
        except websockets.ConnectionClosed:
            print("connection lost, reconnecting...", file=sys.stderr)
            await asyncio.sleep(1)


async def peek(server_url, token, timeout):
    config = _load_config()

    try:
        await asyncio.wait_for(
            _connect_and_handle(server_url, token, config, once=True),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(prog="ghosttrap", description="Watch for errors from ghosttrap.io")
    sub = parser.add_subparsers(dest="command")

    watch_parser = sub.add_parser("watch", help="Stream errors in real time")
    watch_parser.add_argument("--server", default=GHOSTTRAP_SERVER, help="WebSocket server URL")

    peek_parser = sub.add_parser("peek", help="Wait for the next error then exit")
    peek_parser.add_argument("--server", default=GHOSTTRAP_SERVER, help="WebSocket server URL")
    peek_parser.add_argument("--timeout", type=int, default=300, help="Seconds to wait before giving up (exit 1)")

    args = parser.parse_args()

    if args.command == "watch":
        token = get_gh_token()
        asyncio.run(watch(args.server, token))
    elif args.command == "peek":
        token = get_gh_token()
        asyncio.run(peek(args.server, token, args.timeout))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
