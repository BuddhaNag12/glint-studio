"""Fill each catalog file's bytes and sha256 from Hugging Face, without downloading anything.

    uv run --no-sync python -m tools.hf_fill ../catalog/models/*.json    (from host/)

A new file entry needs only "url" and "dest" plus placeholders: "sha256": "", "bytes": 0.
Set HF_TOKEN for gated repos. Edits the text in place, so the file's formatting stays as it is."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import httpx

# A catalog file entry is a flat object: no braces inside it.
ENTRY = re.compile(r'\{[^{}]*"url":\s*"(?P<url>[^"]+)"[^{}]*\}')


def parse_url(url: str) -> tuple[str, str, str]:
    """https://huggingface.co/<owner>/<repo>/resolve/<rev>/<path> → (owner/repo, rev, path)."""
    parts = urlsplit(url)
    segs = parts.path.strip("/").split("/")
    if parts.netloc != "huggingface.co" or len(segs) < 5 or segs[2] != "resolve":
        raise ValueError(f"not a Hugging Face file URL: {url}")
    return f"{segs[0]}/{segs[1]}", segs[3], unquote("/".join(segs[4:]))


def fill(text: str, lookup: Callable[[str], tuple[int, str]]) -> tuple[str, list[str]]:
    """Rewrite bytes and sha256 in every entry; returns the new text and one line per changed file."""
    changes: list[str] = []

    def entry(m: re.Match[str]) -> str:
        url, block = m["url"], m[0]
        if '"dest"' not in block:  # the license link, not a download
            return block
        if '"bytes"' not in block or '"sha256"' not in block:
            raise ValueError(f'add "sha256": "" and "bytes": 0 to the entry for {url}')
        size, sha = lookup(url)
        new = re.sub(r'"bytes":\s*\d+', f'"bytes": {size}', block)
        new = re.sub(r'"sha256":\s*"[^"]*"', f'"sha256": "{sha}"', new)
        if new != block:
            changes.append(f"{PurePosixPath(url).name}: {size} bytes, sha256 {sha[:12]}…")
        return new

    return ENTRY.sub(entry, text), changes


def hf_lookup(client: httpx.Client) -> Callable[[str], tuple[int, str]]:
    """One tree listing per folder; LFS files carry their size and sha256 (the LFS oid)."""
    folders: dict[tuple[str, str, str], dict[str, dict]] = {}

    def lookup(url: str) -> tuple[int, str]:
        repo, rev, path = parse_url(url)
        folder = path.rpartition("/")[0]
        key = (repo, rev, folder)
        if key not in folders:
            res = client.get(f"https://huggingface.co/api/models/{repo}/tree/{rev}/{folder}".rstrip("/"))
            res.raise_for_status()
            folders[key] = {item["path"]: item for item in res.json()}
        item = folders[key].get(path)
        if item is None:
            raise ValueError(f"not found on Hugging Face: {url}")
        if not item.get("lfs"):
            raise ValueError(f"not an LFS file, so Hugging Face has no sha256 for it: {url}")
        return item["lfs"]["size"], item["lfs"]["oid"]

    return lookup


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    token = os.environ.get("HF_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
        lookup = hf_lookup(client)
        for name in argv:
            path = Path(name)
            text, changes = fill(path.read_text(encoding="utf-8"), lookup)
            if changes:
                path.write_text(text, encoding="utf-8")
            print(f"{path.name}: " + ("; ".join(changes) if changes else "up to date"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
