"""ghosttrap CLI — watch for errors streaming from ghosttrap.io."""

import argparse
import asyncio
import json
import subprocess
import sys

import websockets


GHOSTTRAP_SERVER = "wss://ghosttrap.io/stream/"


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


async def watch(server_url, token):
    url = f"{server_url}?token={token}"
    print(f"connecting to {server_url}...", file=sys.stderr)

    async for ws in websockets.connect(url):
        try:
            async for message in ws:
                event = json.loads(message)

                if event.get("type") == "subscribed":
                    repos = event.get("repos", [])
                    print(f"watching {len(repos)} repo(s)", file=sys.stderr)
                    for r in repos:
                        print(f"  {r}", file=sys.stderr)
                    continue

                if event.get("type") == "error":
                    error = event["error"]
                    repo = error.get("repo", "?")
                    etype = error.get("type", "?")
                    msg = error.get("message", "")
                    frames = error.get("frames", [])
                    last_frame = frames[-1] if frames else {}
                    location = f"{last_frame.get('file', '?')}:{last_frame.get('line', '?')}"

                    print(f"\n{'='*60}", file=sys.stderr)
                    print(f"  {repo}", file=sys.stderr)
                    print(f"  {etype}: {msg}", file=sys.stderr)
                    if last_frame:
                        print(f"  at {location} in {last_frame.get('function', '?')}", file=sys.stderr)
                    print(f"{'='*60}", file=sys.stderr)

                    # Full event to stdout for piping / Claude Code consumption
                    print(json.dumps(event))
                    sys.stdout.flush()

        except websockets.ConnectionClosed:
            print("connection lost, reconnecting...", file=sys.stderr)
            continue


def main():
    parser = argparse.ArgumentParser(prog="ghosttrap", description="Watch for errors from ghosttrap.io")
    sub = parser.add_subparsers(dest="command")

    watch_parser = sub.add_parser("watch", help="Stream errors in real time")
    watch_parser.add_argument("--server", default=GHOSTTRAP_SERVER, help="WebSocket server URL")

    args = parser.parse_args()

    if args.command == "watch":
        token = get_gh_token()
        asyncio.run(watch(args.server, token))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
