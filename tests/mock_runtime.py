"""Gerçek-mod sınaması için küçük OpenAI uyumlu sahte çıkarım sunucusu (yalnızca standart kütüphane).

Ajan tarafından   python -m mock_runtime --port N --name ADI   olarak başlatılır (cwd = tests/).
"""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARGS = None
T0 = time.time()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/health":
            if time.time() - T0 < ARGS.load:
                return self._json(503, {"status": "loading"})
            return self._json(200, {"status": "ok"})
        self._json(404, {"error": "yok"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path != ARGS.chat_path:
            return self._json(404, {"error": "yok"})
        last = next((m["content"] for m in reversed(body.get("messages", [])) if m.get("role") == "user"), "")
        words = f"[{ARGS.name}] gerçek-mod sahte yanıtı: {last}".split(" ")
        model = body.get("model", "mock")
        if not body.get("stream"):
            return self._json(200, {"id": "m1", "object": "chat.completion", "model": model,
                                    "choices": [{"index": 0, "message": {"role": "assistant", "content": " ".join(words)}, "finish_reason": "stop"}],
                                    "usage": {"prompt_tokens": 5, "completion_tokens": len(words), "total_tokens": 5 + len(words)}})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def send(obj):
            data = ("data: " + (obj if isinstance(obj, str) else json.dumps(obj)) + "\n\n").encode()
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

        for w in words:
            send({"id": "m1", "object": "chat.completion.chunk", "model": model,
                  "choices": [{"index": 0, "delta": {"content": w + " "}, "finish_reason": None}]})
            time.sleep(0.03)
        send({"id": "m1", "object": "chat.completion.chunk", "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        send({"id": "m1", "object": "chat.completion.chunk", "model": model, "choices": [],
              "usage": {"prompt_tokens": 5, "completion_tokens": len(words), "total_tokens": 5 + len(words)}})
        send("[DONE]")
        self.wfile.write(b"0\r\n\r\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--name", default="mock")
    ap.add_argument("--load", type=float, default=2.0, help="sağlık kontrolünün hazır olacağı süre (sn)")
    ap.add_argument("--chat-path", default="/v1/chat/completions", help="sohbet uç noktası yolu (yamalı sürümleri taklit eder)")
    ARGS = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", ARGS.port), H)
    srv.daemon_threads = True
    print(f"mock_runtime {ARGS.name} port={ARGS.port}", flush=True)
    srv.serve_forever()
