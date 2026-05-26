#!/usr/bin/env python3
"""Freeze the most recent release's SKILL_CONTENT hash into KNOWN_SKILL_HASHES.

Run before bumping the version. Looks at the latest git tag, hashes that
tag's bundled SKILL_CONTENT, and inserts it into KNOWN_SKILL_HASHES in
ghosttrap_cli/cli.py so the next release can detect and refresh users
running the previous version.
"""

import hashlib
import re
import subprocess
import sys
from pathlib import Path

CLI_PATH = Path(__file__).resolve().parent.parent / "ghosttrap_cli" / "cli.py"
SKILL_RE = re.compile(r'SKILL_CONTENT\s*=\s*"""\\?\n(.*?)"""', re.DOTALL)
SET_RE = re.compile(r"(KNOWN_SKILL_HASHES = \{\n)(.*?)(\})", re.DOTALL)


def extract_skill(source):
    m = SKILL_RE.search(source)
    if not m:
        sys.exit("could not find SKILL_CONTENT in cli.py")
    return m.group(1)


def latest_tag():
    return subprocess.check_output(
        ["git", "describe", "--tags", "--abbrev=0"], text=True
    ).strip()


def skill_at_tag(tag):
    src = subprocess.check_output(
        ["git", "show", f"{tag}:ghosttrap_cli/cli.py"], text=True
    )
    return extract_skill(src)


def main():
    tag = latest_tag()
    prev_hash = hashlib.sha256(skill_at_tag(tag).encode()).hexdigest()
    source = CLI_PATH.read_text()
    cur_hash = hashlib.sha256(extract_skill(source).encode()).hexdigest()

    if prev_hash == cur_hash:
        print(f"skill unchanged since {tag}; nothing to freeze")
        return
    if prev_hash in source:
        print(f"hash for {tag} already frozen")
        return

    m = SET_RE.search(source)
    if not m:
        sys.exit("could not find KNOWN_SKILL_HASHES set")
    new_entry = f'    "{prev_hash}",  # {tag}\n'
    new_source = source[: m.end(2)] + new_entry + source[m.end(2):]
    CLI_PATH.write_text(new_source)
    print(f"froze {tag} hash {prev_hash[:12]}...")


if __name__ == "__main__":
    main()
