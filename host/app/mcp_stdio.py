"""stdio to HTTP bridge for MCP clients that only start local servers (Claude Desktop).

Reads one JSON-RPC message per line on stdin, POSTs it to GLINT_URL/mcp with GLINT_API_KEY, and
writes the answer to stdout. Standard library only, so it runs with any Python 3.10+, no Node.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request


def forward(line: str, url: str, key: str, timeout: float = 900) -> str | None:
    """The answer to write back for one message, or None for a notification."""
    try:
        msg = json.loads(line)
    except ValueError:
        return json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error."}})
    req = urllib.request.Request(
        url.rstrip("/") + "/mcp",
        data=line.encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                 "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            body = res.read().decode("utf-8")
            return body or None
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read())["error"]
            detail = f"{error['code']}: {error['message']}"
        except (ValueError, KeyError, TypeError):
            detail = f"HTTP {exc.code}"
        problem = f"Glint Studio refused the request ({detail})."
    except OSError:
        problem = "Glint Studio isn't running. Open it, then try again."
    if not isinstance(msg, dict) or "id" not in msg:
        return None
    return json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32603, "message": problem}})


def main() -> None:
    url, key = os.environ.get("GLINT_URL", "http://127.0.0.1:7788"), os.environ.get("GLINT_API_KEY", "")
    lock = threading.Lock()

    def handle(line: str) -> None:
        answer = forward(line, url, key)
        if answer:
            with lock:
                sys.stdout.write(answer.replace("\n", " ") + "\n")
                sys.stdout.flush()

    # A thread per message: a ping still gets its answer while an image is being made.
    threads = []
    for line in sys.stdin:
        if line.strip():
            threads.append(threading.Thread(target=handle, args=(line.strip(),), daemon=True))
            threads[-1].start()
    for thread in threads:  # stdin closed: finish what was asked
        thread.join()


if __name__ == "__main__":
    main()
