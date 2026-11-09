"""Empirically verify curl's redirect credential behavior on loopback only.

No external host is contacted; the canary is synthetic. The local servers
record whether the redirected request contains the canary.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CANARY = "synthetic-canary-123"


def main() -> None:
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise SystemExit("curl executable not found")
    received: list[tuple[str, dict[str, str]]] = []
    destination = ""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/report":
                self.send_response(302)
                self.send_header("Location", destination)
                self.end_headers()
            else:
                received.append((str(self.server.server_address), dict(self.headers)))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"synthetic report")

        def log_message(self, *_args: object) -> None:
            pass

    origin = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    collector = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    for server in (origin, collector):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    origin_url = f"http://127.0.0.1:{origin.server_port}"
    collector_url = f"http://127.0.0.1:{collector.server_port}"
    scenarios = [
        ("cookie-header", ["-L", "-H", f"Cookie: session={CANARY}"], False, False),
        ("custom-session-header", ["-L", "-H", f"X-Session: {CANARY}"], False, True),
        ("header-file-authorization", ["-L", "-H", "@headers.txt"], False, False),
        ("header-file-custom", ["-L", "-H", "@headers.txt"], False, True),
        ("trusted-same-origin", ["--location-trusted", "-H", f"Authorization: Bearer {CANARY}"], True, True),
        ("trusted-cross-origin", ["--location-trusted", "-H", f"Authorization: Bearer {CANARY}"], False, True),
    ]
    rows = []
    try:
        with tempfile.TemporaryDirectory(prefix="curl-oracle-") as temporary:
            root = Path(temporary)
            for name, flags, same_origin, expected_canary in scenarios:
                destination = f"{origin_url if same_origin else collector_url}/final"
                received.clear()
                if name == "header-file-authorization":
                    (root / "headers.txt").write_text(f"Authorization: Bearer {CANARY}\n", encoding="utf-8")
                elif name == "header-file-custom":
                    (root / "headers.txt").write_text(f"X-API-Key: {CANARY}\n", encoding="utf-8")
                command = [curl, "-q", *flags, f"{origin_url}/report", "--silent", "--show-error", "--max-time", "5", "--output", "NUL"]
                result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=10, check=False)
                if result.returncode != 0 or len(received) != 1:
                    raise RuntimeError(f"{name}: curl failed: {result.returncode} {result.stderr!r}; received={received!r}")
                _, headers = received[0]
                canary_seen = any(CANARY in value for value in headers.values())
                rows.append({"scenario": name, "redirected_request_canary": canary_seen, "expected_canary": expected_canary, "redirected_headers": headers, "curl_version": subprocess.check_output([curl, "--version"], text=True).splitlines()[0]})
    finally:
        origin.shutdown()
        collector.shutdown()
        origin.server_close()
        collector.server_close()
    print(json.dumps(rows, indent=2))
    if any(row["redirected_request_canary"] != row["expected_canary"] for row in rows):
        raise SystemExit("At least one expected canary observation was wrong")


if __name__ == "__main__":
    main()
