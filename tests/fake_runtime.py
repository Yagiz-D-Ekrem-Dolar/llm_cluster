#!/usr/bin/env python3
"""Sınama için llama-server / whisper-server taklidi. Runtime paketine konur, ajan gerçek süreç olarak başlatır.

llama.cpp komut satırını kabul eder ve verilen model dosyasını gerçekten okur: doğru dosya makineye ulaşmadıysa yanıt vermez.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ap = argparse.ArgumentParser(add_help=False)
for flag in ("-m", "--host", "--port", "-c", "-ngl", "--alias", "-np", "-t", "-l", "--inference-path"):
    ap.add_argument(flag)
A, _ = ap.parse_known_args()
if not A.m or not os.path.exists(A.m):
    sys.exit(f"model dosyası yok: {A.m}")
with open(A.m, "rb") as f:
    MODEL_SHA = hashlib.sha256(f.read(1 << 20)).hexdigest()[:12]
MODE = "whisper" if "whisper" in os.path.basename(sys.argv[0]) else "llama"
NAME = A.alias or "model"
BIND = os.environ.get("TEST_BIND", "127.0.0.1")
T0 = time.time()
ACTIVE = [0]
print(f"fake {MODE} {NAME} ctx={A.c} np={A.np} ngl={A.ngl} model={A.m}", flush=True)


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/health":
            return self._json(200 if time.time() - T0 > 0.6 else 503, {"status": "ok"})
        if self.path == "/active":
            return self._json(200, {"active": ACTIVE[0]})
        self._json(404, {"error": "yok"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        if self.path == "/v1/audio/transcriptions":
            m = re.search(rb'name="language"\r\n\r\n([^\r]*)', raw)
            return self._json(200, {"text": f"[{NAME}] {len(raw)} bayt", "language": (m.group(1).decode() if m else "")})
        if self.path != "/v1/chat/completions":
            return self._json(404, {"error": "yok"})
        body = json.loads(raw or b"{}")
        last = next((x["content"] for x in reversed(body.get("messages", [])) if x.get("role") == "user"), "")
        limit = int(body.get("max_tokens") or 40)
        words = f"[{NAME}|{MODEL_SHA}|{BIND}] yanıt: {last}".split(" ")[:limit]
        if not body.get("stream"):
            return self._json(200, {"id": "c1", "object": "chat.completion", "model": body.get("model"),
                                    "choices": [{"index": 0, "message": {"role": "assistant", "content": " ".join(words)}, "finish_reason": "stop"}],
                                    "usage": {"prompt_tokens": 7, "completion_tokens": len(words), "total_tokens": 7 + len(words)}})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def send(o):
            d = ("data: " + (o if isinstance(o, str) else json.dumps(o, ensure_ascii=False)) + "\n\n").encode()
            self.wfile.write(b"%x\r\n%s\r\n" % (len(d), d))
            self.wfile.flush()

        ACTIVE[0] += 1
        try:
            delay = float(os.environ.get("FAKE_TOKEN_DELAY", "0.03"))
            if "yavas" in str(last):
                words = words + ["kelime"] * 300
                delay = 0.05
            for w in words:
                send({"id": "c1", "object": "chat.completion.chunk", "model": body.get("model"), "choices": [{"index": 0, "delta": {"content": w + " "}, "finish_reason": None}]})
                time.sleep(delay)
            send({"id": "c1", "object": "chat.completion.chunk", "model": body.get("model"), "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            send({"id": "c1", "object": "chat.completion.chunk", "model": body.get("model"), "choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": len(words), "total_tokens": 7 + len(words)}})
            send("[DONE]")
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionError):
            self.close_connection = True
        finally:
            ACTIVE[0] -= 1


srv = ThreadingHTTPServer((BIND, int(A.port)), H)
srv.daemon_threads = True
srv.serve_forever()
