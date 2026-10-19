import http.server
import os
import webbrowser
import urllib.request

os.chdir(os.path.dirname(os.path.abspath(__file__)))

BASE = "http://localhost:3001/api"
KEY = "Bearer 你的api_key"
PORT = 8000


class Handler(http.server.SimpleHTTPRequestHandler):
    def _proxy(self, method):
        path = self.path[len("/api"):]
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None
        ctype = self.headers.get("Content-Type", "")

        req = urllib.request.Request(
            BASE + path, data=body, method=method,
            headers={"Authorization": KEY},
        )
        if body is not None and ctype:
            req.add_header("Content-Type", ctype)

        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                payload, status = resp.read(), resp.status
        except urllib.error.HTTPError as e:
            payload, status = e.read(), e.code

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/":
            self.path = "/index.html"
        if self.path.startswith("/api"):
            return self._proxy("GET")
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api"):
            return self._proxy("POST")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    webbrowser.open(f"http://127.0.0.1:{PORT}/")
    print(f"Running on http://127.0.0.1:{PORT}/")
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
