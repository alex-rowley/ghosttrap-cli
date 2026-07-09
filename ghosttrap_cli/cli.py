"""ghosttrap CLI — watch for errors streaming from ghosttrap.io."""

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import websockets


def _harden_signals():
    """Explicitly ignore SIGURG so no process supervisor can nudge peek out
    with an out-of-band signal. POSIX default is already ignore; this makes
    it defensive against layers that change the disposition.
    """
    s = getattr(signal, "SIGURG", None)
    if s is not None:
        try:
            signal.signal(s, signal.SIG_IGN)
        except (OSError, ValueError):
            pass

__version__ = "0.3.21"

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
- `repos`: map keyed by GitHub repo id (stringified int) to `{"github_id": int, "owner": str, "name": str, "token": "t_xxx", "sdk_installed": bool, "sdk_version": str, "init_file": str}`. Entries may also carry `recent`, the error ids cached by the last `ghosttrap list` (managed by the CLI — leave it alone).
- `cursor`: last seen error ID
- `skill_baseline`: the previous release's skill text, used to 3-way-merge skill updates with local edits. Never edit or delete it.

## On session start

1. Detect the current repo from `git config --get remote.origin.url` (returns `owner/name`).
2. Find a matching entry in config by looking for one whose `owner`/`name` equals the detected slug. If no match, tell the user to run `ghosttrap setup`. (The owner/name on a config entry auto-refreshes from the server when the repo is renamed or transferred, so always match against the entry's stored owner/name, not the config key.)
3. If `sdk_installed` is false or missing: install the SDK (`pip install ghosttrap-sdk`), wire `ghosttrap.init("<token>")` into the app startup. For Django projects, also add `"ghosttrap.django.GhostTrapApp"` to INSTALLED_APPS (re-attaches logging handler after Django's dictConfig) and `"ghosttrap.django.GhostTrapMiddleware"` to MIDDLEWARE (catches unhandled view exceptions). The SDK auto-hooks into Celery task_failure if Celery is installed, and attaches a logging handler for logger.exception() calls. Use whatever pattern the project already uses for configuration (env vars, settings files, hardcoded — match the existing style). Then update the config: set `sdk_installed: true`, `sdk_version`, `init_file` to record what you did. Only pass `send_user=True` to `init()` if the user explicitly asks for user context in reports — it's PII and stays off by default.
4. Run `ghosttrap peek --clear` with `run_in_background: true`. The `--clear` flag skips any stale backlog from prior sessions so you only get fresh errors.

## Manual capture

For caught exceptions or non-exception conditions the user explicitly wants reported, use `ghosttrap.trap(exc_or_message)` from app code — pass an exception instance or a string. Synthetic string events arrive as type `TrappedEvent` with the caller's stack. Only wire this in when the user asks for it; don't add `trap()` calls speculatively.

## When peek returns

1. **Immediately restart peek** in the background before doing anything else — this ensures you're listening for the next error while you work on the current one. Use plain `ghosttrap peek` here (no `--clear`) — you only want to skip backlog at session start.
2. Read the JSON output: `error.repo`, `error.type`, `error.message`, `error.traceback` (list of strings), `error.frames` (list of `{file, line, function, code}`).
3. Open the file from the last frame, diagnose, fix.

## Other commands

- `ghosttrap last` — fetch the single most recent error and exit immediately, no waiting. Useful when the user wants to look at the latest error without blocking on a peek. Add `--clear` to also skip everything older in one shot.
- `ghosttrap list [n]` — print a numbered summary of the most recent `n` errors (default 10, max 50). Does not move the cursor. Caches the ordered ids in config so a follow-up `ghosttrap show <i>` returns full details for that row.
- `ghosttrap show <i>` — full details for the i-th row from the most recent `ghosttrap list`. Does not move the cursor.
- `ghosttrap clear` — manually skip outstanding errors without waiting. Useful if the user explicitly wants to drop the queue.
- `ghosttrap nuke` — permanently delete every server-side row for the current repo (errors + the Repo row + its token). Requires the user to type the repo name `owner/name` to confirm. Only run if the user explicitly asks to wipe server data — never proactively. After it succeeds the token is dead; the user would need to `ghosttrap setup` again to use this repo.

`peek` and every command above except `nuke` accept `--repo owner/name` to target another claimed repo when the cwd isn't inside it (e.g. `ghosttrap list --repo owner/name`).

## Rules

- Always `run_in_background: true` for peek — it blocks.
- Don't run multiple peeks at once.
- Peek reconnects by itself (60s backoff) when the connection drops — a quiet peek is waiting, not hung. It only exits after printing an error event, or with a message on stderr if something is actually wrong; restart it only in that second case.
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


def _get_repo_entry(config, requested=None):
    """Return (key, entry) for the chosen repo. Same resolution rules as _get_repo_token."""
    repos = config.get("repos", {})
    if not repos:
        print("error: no repos configured. run 'ghosttrap setup' first.", file=sys.stderr)
        sys.exit(1)
    if requested:
        for k, entry in repos.items():
            if f"{entry.get('owner')}/{entry.get('name')}" == requested:
                return k, entry
        available = sorted(
            f"{e.get('owner')}/{e.get('name')}"
            for e in repos.values()
            if e.get('owner') and e.get('name')
        )
        print(f"error: '{requested}' is not in your config.", file=sys.stderr)
        if available:
            print(f"available: {', '.join(available)}", file=sys.stderr)
        sys.exit(1)
    cwd_repo = _detect_repo_from_cwd()
    if cwd_repo:
        for k, entry in repos.items():
            if f"{entry.get('owner')}/{entry.get('name')}" == cwd_repo:
                return k, entry
    k = next(iter(repos))
    return k, repos[k]


def _get_repo_token(config, requested=None):
    """Get the repo token. If `requested` is 'owner/name', match strictly. Else cwd, else first."""
    _, entry = _get_repo_entry(config, requested)
    return entry["token"]


async def _connect_and_handle(server_url, token, config, once=False):
    """Core WebSocket loop. If once=True, returns True after the first error event.
    Returns False if the server closed the socket without sending an error
    (e.g. idle timeout) so callers can distinguish 'job done' from 'reconnect me'.
    """
    since = config.get("cursor")
    url = f"{server_url}?token={token}"
    if since is not None:
        url += f"&since={since}"

    async with websockets.connect(url) as ws:
        async for message in ws:
            event = json.loads(message)

            if event.get("type") == "rejected":
                print(f"error: {event.get('message', event.get('code', 'rejected by server'))}", file=sys.stderr)
                sys.exit(1)

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
                    return True
    return False


def _require_setup():
    if not os.path.exists(CONFIG_FILE):
        print("error: ghosttrap is not set up. run 'ghosttrap setup' first.", file=sys.stderr)
        sys.exit(1)


def _write_skill(config=None):
    os.makedirs(SKILL_DIR, exist_ok=True)
    with open(SKILL_FILE, "w") as f:
        f.write(SKILL_CONTENT)
    if config is None:
        config = _load_config()
    config["skill_baseline"] = SKILL_CONTENT
    _save_config(config)


def _merge_skill(base, local, remote):
    """3-way merge via `git merge-file -p`. Returns (merged_text, clean)."""
    with tempfile.TemporaryDirectory() as d:
        bp = os.path.join(d, "base")
        lp = os.path.join(d, "local")
        rp = os.path.join(d, "remote")
        for path, text in [(bp, base), (lp, local), (rp, remote)]:
            with open(path, "w") as f:
                f.write(text)
        result = subprocess.run(
            ["git", "merge-file", "-p",
             "-L", "your edits", "-L", "previous release", "-L", "new release",
             lp, bp, rp],
            capture_output=True, text=True,
        )
        return result.stdout, result.returncode == 0


def _refresh_skill_if_stale():
    if not os.path.exists(SKILL_FILE):
        return
    with open(SKILL_FILE) as f:
        on_disk = f.read()
    if on_disk == SKILL_CONTENT:
        return
    config = _load_config()
    baseline = config.get("skill_baseline")
    if baseline is None:
        # Pre-baseline install: adopt current on-disk content as the baseline
        # so future releases can 3-way-merge instead of clobbering local edits.
        config["skill_baseline"] = on_disk
        _save_config(config)
        return
    if baseline == on_disk:
        _write_skill(config)
        print("ghosttrap skill file updated", file=sys.stderr)
        return
    merged, clean = _merge_skill(baseline, on_disk, SKILL_CONTENT)
    if clean:
        with open(SKILL_FILE, "w") as f:
            f.write(merged)
        config["skill_baseline"] = SKILL_CONTENT
        _save_config(config)
        print("ghosttrap skill file updated (merged with your local edits)", file=sys.stderr)
        return
    new_path = SKILL_FILE + ".new"
    with open(new_path, "w") as f:
        f.write(merged)
    print(
        f"ghosttrap skill update has conflicts with your local edits; "
        f"merged candidate at {new_path} — resolve, copy to {SKILL_FILE}, and rerun.",
        file=sys.stderr,
    )


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

            if event.get("type") == "rejected":
                print(f"error: {event.get('message', event.get('code', 'rejected by server'))}", file=sys.stderr)
                sys.exit(1)

            if event.get("type") != "subscribed":
                print(f"error: unexpected response from server: {event}", file=sys.stderr)
                sys.exit(1)

            repos = event.get("repos", [])
            _save_repos(config, repos)
            _write_skill(config)

            target = repos[0] if repos else None

            print(f"claimed {cwd_repo}", file=sys.stderr)
            print(f"skill file written to {SKILL_FILE}", file=sys.stderr)

            if target:
                _print_setup_snippet(target)

            print("done — Claude Code will take it from here\n", file=sys.stderr)

    except SystemExit:
        raise
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


# Exception types we knowingly retry on. All represent a transient network/transport
# blip rather than a semantic error from the server:
#   - ConnectionClosed: peer closed the WebSocket (either side of the handshake)
#   - InvalidStatus:    non-101 HTTP response during upgrade (e.g. 502 from a proxy)
#   - ConnectionError:  builtin — refused, reset, unreachable
#   - OSError:          DNS failure, transient socket errors (gaierror is a subclass)
# Anything else escapes and prints a diagnostic line first so we can add it here
# in the next release. Semantic rejections from the server ({"type": "rejected"})
# raise SystemExit, which we deliberately do NOT catch — those are real errors.
_RETRYABLE = (
    websockets.ConnectionClosed,
    websockets.InvalidStatus,
    ConnectionError,
    OSError,
)


def _log_unexpected(e):
    print(
        f"unexpected {type(e).__module__}.{type(e).__name__}: {e} — "
        f"not currently in the retry list; please report so we can add it.",
        file=sys.stderr,
    )


async def watch(server_url, token):
    config = _load_config()
    print(f"connecting to {server_url}...", file=sys.stderr)

    while True:
        try:
            await _connect_and_handle(server_url, token, config, once=False)
            print("connection closed by server, reconnecting...", file=sys.stderr)
        except _RETRYABLE:
            print("connection lost, reconnecting...", file=sys.stderr)
        except Exception as e:
            _log_unexpected(e)
            raise
        await asyncio.sleep(60)


async def peek(server_url, token):
    config = _load_config()
    _check_cli_version(config)
    while True:
        try:
            got_error = await _connect_and_handle(server_url, token, config, once=True)
            if got_error:
                return
            print("connection closed by server, reconnecting...", file=sys.stderr)
        except _RETRYABLE:
            print("connection lost, reconnecting...", file=sys.stderr)
        except Exception as e:
            _log_unexpected(e)
            raise
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


def clear(requested=None):
    _require_setup()
    config = _load_config()
    token = _get_repo_token(config, requested)
    try:
        pending = _advance_cursor(config, token)
        if pending:
            print(f"cleared {pending} error(s)", file=sys.stderr)
        else:
            print(f"nothing to clear", file=sys.stderr)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


def nuke():
    _require_setup()
    config = _load_config()
    repos = config.get("repos", {})
    cwd_repo = _detect_repo_from_cwd()
    if not cwd_repo:
        print("error: not in a git repo with a github remote", file=sys.stderr)
        sys.exit(1)

    entry_key = None
    entry = None
    for k, e in repos.items():
        if f"{e.get('owner')}/{e.get('name')}" == cwd_repo:
            entry_key, entry = k, e
            break
    if entry is None:
        print(f"error: {cwd_repo} is not in your config. run 'ghosttrap setup' to claim it first.", file=sys.stderr)
        sys.exit(1)

    canonical = f"{entry['owner']}/{entry['name']}"
    print(f"\nthis will permanently delete ALL data on the server for {canonical}:", file=sys.stderr)
    print(f"  - every Error row for this repo", file=sys.stderr)
    print(f"  - the Repo row itself (token will stop working)", file=sys.stderr)
    print(f"\ntype the repo name to confirm: ", file=sys.stderr, end="")
    sys.stderr.flush()
    try:
        typed = input().strip()
    except (EOFError, KeyboardInterrupt):
        print("\naborted", file=sys.stderr)
        sys.exit(1)
    if typed != canonical:
        print("aborted (did not match)", file=sys.stderr)
        sys.exit(1)

    server = GHOSTTRAP_SERVER.replace("wss://", "https://").replace("/stream/", "")
    url = f"{server}/nuke/{entry['token']}/"
    try:
        req = urllib.request.Request(url, method="DELETE", headers={"User-Agent": "ghosttrap-cli"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(data))
    print(f"\nnuked {data.get('repo')}:", file=sys.stderr)
    print(f"  errors deleted: {data.get('errors_deleted')}", file=sys.stderr)
    print(f"  repos deleted:  {data.get('repo_deleted')}", file=sys.stderr)

    repos.pop(entry_key, None)
    _save_config(config)


def _rel_time(iso):
    if not iso:
        return "?"
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        s = int(delta.total_seconds())
        if s < 60:
            return f"{s}s ago"
        if s < 3600:
            return f"{s // 60}m ago"
        if s < 86400:
            return f"{s // 3600}h ago"
        if s < 86400 * 30:
            return f"{s // 86400}d ago"
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return iso


def _print_error_details(error):
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


def list_recent(n=10, requested=None):
    _require_setup()
    config = _load_config()
    _check_cli_version(config)
    n = max(1, min(int(n), 50))
    key, entry = _get_repo_entry(config, requested)
    token = entry["token"]
    server = GHOSTTRAP_SERVER.replace("wss://", "https://").replace("/stream/", "")
    url = f"{server}/list/{token}/?n={n}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ghosttrap-cli"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    errors = data.get("errors", [])
    if not errors:
        print("no errors yet", file=sys.stderr)
        entry["recent"] = []
        _save_config(config)
        return

    entry["recent"] = [e["id"] for e in errors]
    _save_config(config)

    width = len(str(len(errors)))
    for i, e in enumerate(errors, 1):
        when = _rel_time(e.get("created_at"))
        etype = e.get("type") or "?"
        msg = (e.get("message") or "").splitlines()[0] if e.get("message") else ""
        if len(msg) > 60:
            msg = msg[:57] + "..."
        loc = ""
        if e.get("file"):
            loc = f"{e['file']}:{e.get('line', '?')}"
            if e.get("function"):
                loc += f" ({e['function']})"
        print(f"  {i:>{width}}  {when:<12}  {etype:<20}  {msg:<60}  {loc}")
    print(f"\nrun 'ghosttrap show <n>' to see full details. cursor unchanged.", file=sys.stderr)


def show(index, requested=None):
    _require_setup()
    config = _load_config()
    _check_cli_version(config)
    key, entry = _get_repo_entry(config, requested)
    recent = entry.get("recent") or []
    if not recent:
        print("error: no recent list cached. run 'ghosttrap list' first.", file=sys.stderr)
        sys.exit(1)
    try:
        i = int(index)
    except (TypeError, ValueError):
        print(f"error: '{index}' is not a number.", file=sys.stderr)
        sys.exit(1)
    if i < 1 or i > len(recent):
        print(f"error: index out of range. last list had {len(recent)} entries.", file=sys.stderr)
        sys.exit(1)
    db_id = recent[i - 1]
    token = entry["token"]
    server = GHOSTTRAP_SERVER.replace("wss://", "https://").replace("/stream/", "")
    url = f"{server}/error/{token}/{db_id}/"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ghosttrap-cli"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"error: this error no longer exists on the server (id #{db_id}).", file=sys.stderr)
        else:
            print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    error = data.get("error")
    if not error:
        print(f"error: empty response", file=sys.stderr)
        sys.exit(1)
    _print_error_details(error)


def last(do_clear=False, requested=None):
    _require_setup()
    config = _load_config()
    _check_cli_version(config)
    token = _get_repo_token(config, requested)
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

    _print_error_details(error)

    if do_clear:
        try:
            _advance_cursor(config, token)
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)


def main():
    _harden_signals()
    parser = argparse.ArgumentParser(prog="ghosttrap", description="Watch for errors from ghosttrap.io")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("setup", help="Claim repos and install Claude Code skill")

    clear_parser = sub.add_parser("clear", help="Skip all outstanding errors")
    clear_parser.add_argument("--repo", help="Target repo as owner/name (overrides cwd detection)")

    watch_parser = sub.add_parser("watch", help="Deprecated: stream errors in real time (use peek)")
    watch_parser.add_argument("--server", default=GHOSTTRAP_SERVER, help="WebSocket server URL")
    watch_parser.add_argument("--repo", help="Target repo as owner/name (overrides cwd detection)")

    peek_parser = sub.add_parser("peek", help="Wait for the next error then exit")
    peek_parser.add_argument("--server", default=GHOSTTRAP_SERVER, help="WebSocket server URL")
    peek_parser.add_argument("--clear", action="store_true", help="Skip outstanding errors before waiting")
    peek_parser.add_argument("--repo", help="Target repo as owner/name (overrides cwd detection)")

    last_parser = sub.add_parser("last", help="Fetch the most recent error then exit")
    last_parser.add_argument("--clear", action="store_true", help="Also skip remaining outstanding errors")
    last_parser.add_argument("--repo", help="Target repo as owner/name (overrides cwd detection)")

    list_parser = sub.add_parser("list", help="List the most recent N errors (summary only, cursor unchanged)")
    list_parser.add_argument("n", nargs="?", type=int, default=10, help="How many to list (default 10, max 50)")
    list_parser.add_argument("--repo", help="Target repo as owner/name (overrides cwd detection)")

    show_parser = sub.add_parser("show", help="Show full details for an index from the last 'list' (cursor unchanged)")
    show_parser.add_argument("index", type=int, help="1-based index from the last 'ghosttrap list'")
    show_parser.add_argument("--repo", help="Target repo as owner/name (overrides cwd detection)")

    sub.add_parser("nuke", help="Permanently delete all server data for the current repo")

    args = parser.parse_args()

    if args.command == "setup":
        token = get_gh_token()
        asyncio.run(setup(GHOSTTRAP_SERVER, token))
    elif args.command == "clear":
        clear(requested=args.repo)
    elif args.command == "watch":
        print(
            "warning: 'watch' is deprecated and may be removed in a future release — "
            "'peek' now reconnects until an error arrives.",
            file=sys.stderr,
        )
        _require_setup()
        _refresh_skill_if_stale()
        config = _load_config()
        token = _get_repo_token(config, args.repo)
        asyncio.run(watch(args.server, token))
    elif args.command == "peek":
        _require_setup()
        _refresh_skill_if_stale()
        config = _load_config()
        token = _get_repo_token(config, args.repo)
        if args.clear:
            try:
                _advance_cursor(config, token)
            except Exception as e:
                print(f"error: {e}", file=sys.stderr)
                sys.exit(1)
        asyncio.run(peek(args.server, token))
    elif args.command == "last":
        _refresh_skill_if_stale()
        last(do_clear=args.clear, requested=args.repo)
    elif args.command == "list":
        _refresh_skill_if_stale()
        list_recent(n=args.n, requested=args.repo)
    elif args.command == "show":
        _refresh_skill_if_stale()
        show(args.index, requested=args.repo)
    elif args.command == "nuke":
        nuke()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
