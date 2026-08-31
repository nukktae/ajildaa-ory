#!/usr/bin/env python3
"""
Tiny stdlib HTTP server for browsing the scraped postings.

    python serve.py            # http://localhost:8000
    python serve.py --port 9000 --data data

Serves web/index.html and a compact /api/postings.json built from
data/postings.jsonl. The whole (small) dataset is sent once so that
search/filter/sort happen client-side with no round trip per keystroke.
"""

import argparse, json, os, re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TAG_RE = re.compile(r"<[^>]+>")
HERE = os.path.dirname(os.path.abspath(__file__))

FIELDS = ("id", "url", "title", "start_time", "end_time", "image_url",
          "employment_page_url", "view_count", "favorite_count", "first_seen")


def load(path):
    """Read postings.jsonl -> list of slim dicts for the browser."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            rec = {k: r.get(k) for k in FIELDS}
            rec["company"] = r.get("name")
            rec["roles"] = [e.get("field") for e in (r.get("employments") or [])
                            if e.get("field")]
            out.append(rec)
    out.sort(key=lambda r: r.get("end_time") or "")
    return out


class Handler(BaseHTTPRequestHandler):
    data_path = ""
    _cache = {"mtime": None, "body": b""}

    def _send(self, body, ctype, cache="no-store"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "web", "index.html"), "rb") as f:
                return self._send(f.read(), "text/html; charset=utf-8")
        if path == "/api/postings.json":
            try:
                mtime = os.path.getmtime(self.data_path)
            except OSError:
                self.send_error(500, "postings.jsonl not found — run scrape.py first")
                return
            if self._cache["mtime"] != mtime:      # reload only when the file changed
                rows = load(self.data_path)
                self._cache["body"] = json.dumps(
                    {"postings": rows, "count": len(rows)},
                    ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self._cache["mtime"] = mtime
            return self._send(self._cache["body"], "application/json; charset=utf-8")
        self.send_error(404)

    def log_message(self, fmt, *args):
        pass  # quiet


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--data", default=os.path.join(HERE, "data"))
    p.add_argument("--host", default="127.0.0.1")
    a = p.parse_args()

    Handler.data_path = os.path.join(a.data, "postings.jsonl")
    if not os.path.exists(Handler.data_path):
        raise SystemExit(f"no data at {Handler.data_path} — run scrape.py first")
    n = len(load(Handler.data_path))
    print(f"{n} postings — http://{a.host}:{a.port}")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
