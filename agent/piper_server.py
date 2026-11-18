#!/usr/bin/env python3
"""Piper için OpenAI uyumlu konuşma sunucusu: POST /v1/audio/speech  ·  GET /health

Ajan bu dosyayı runtime paketi olarak alır ve çalıştırır. Piper kurulu Python ortamı varsa kendini onunla yeniden başlatır.
Sesi cümle cümle üretip akış halinde gönderir; ilk ses kısa sürede duyulur. mp3/opus için makinede ffmpeg gerekir.
"""
import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VENV_PYTHONS = ("/opt/orkestra/piper/bin/python", "/opt/orkestra/piper/bin/python3")
FORMATS = {"wav": "audio/wav", "pcm": "audio/L16", "mp3": "audio/mpeg", "opus": "audio/ogg", "aac": "audio/aac", "flac": "audio/flac"}
FFMPEG = {"mp3": ["-f", "mp3", "-b:a", "96k"], "opus": ["-f", "ogg", "-c:a", "libopus", "-b:a", "48k"], "aac": ["-f", "adts", "-b:a", "96k"], "flac": ["-f", "flac"]}
ARGS = None
VOICE = None
RATE = 22050
GATE = None


def wav_header(rate: int) -> bytes:
    """Uzunluğu bilinmeyen akış için WAV başlığı."""
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", 0xFFFFFFFF))


def split_text(text: str):
    parts = re.split(r"(?<=[.!?…:;])\s+|\n+", text.strip())
    return [p.strip() for p in parts if p.strip()] or [text]


def synth(text: str, speed: float):
    """Kurulu Piper sürümüne göre ham 16 bit PCM parçaları üretir."""
    scale = 1.0 / max(0.25, min(4.0, speed or 1.0))
    if hasattr(VOICE, "synthesize_stream_raw"):
        yield from VOICE.synthesize_stream_raw(text, length_scale=scale)
        return
    try:
        from piper import SynthesisConfig
        cfg = SynthesisConfig(length_scale=scale)
    except Exception:
        cfg = None
    for chunk in (VOICE.synthesize(text, syn_config=cfg) if cfg is not None else VOICE.synthesize(text)):
        yield chunk.audio_int16_bytes


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

    def _chunk(self, b):
        if b:
            self.wfile.write(b"%x\r\n%s\r\n" % (len(b), b))
            self.wfile.flush()

    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            return self._json(200, {"status": "ok", "sample_rate": RATE})
        if self.path.split("?")[0] == "/v1/models":
            return self._json(200, {"object": "list", "data": [{"id": os.path.basename(ARGS.model), "object": "model"}]})
        self._json(404, {"error": {"message": "bulunamadı"}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json(400, {"error": {"message": "geçersiz JSON"}})
        if self.path.split("?")[0] != "/v1/audio/speech":
            return self._json(404, {"error": {"message": "bulunamadı"}})
        text = str(body.get("input") or "").strip()
        fmt = str(body.get("response_format") or "wav").lower()
        if not text:
            return self._json(400, {"error": {"message": "input boş"}})
        if fmt not in FORMATS:
            return self._json(400, {"error": {"message": f"desteklenmeyen biçim: {fmt}"}})
        if fmt in FFMPEG and not shutil.which("ffmpeg"):
            return self._json(400, {"error": {"message": f"{fmt} için makinede ffmpeg gerekli; wav isteyin ya da ffmpeg kurun"}})
        speed = float(body.get("speed") or 1.0)
        self.send_response(200)
        self.send_header("Content-Type", FORMATS[fmt])
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.close_connection = True
        with GATE:
            try:
                if fmt in FFMPEG:
                    self._via_ffmpeg(text, speed, fmt)
                else:
                    if fmt == "wav":
                        self._chunk(wav_header(RATE))
                    for sent in split_text(text):
                        for pcm in synth(sent, speed):
                            self._chunk(pcm)
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionError):
                pass

    def _via_ffmpeg(self, text, speed, fmt):
        p = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "pipe:0"] + FFMPEG[fmt] + ["pipe:1"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)

        def feed():
            try:
                for sent in split_text(text):
                    for pcm in synth(sent, speed):
                        p.stdin.write(pcm)
                p.stdin.close()
            except Exception:
                p.kill()

        threading.Thread(target=feed, daemon=True).start()
        try:
            while True:
                b = p.stdout.read(16384)
                if not b:
                    break
                self._chunk(b)
        finally:
            p.kill()
            p.wait()


def main():
    global ARGS, VOICE, RATE, GATE
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help=".onnx ses dosyası ya da onu içeren klasör")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--parallel", type=int, default=2)
    ARGS = ap.parse_args()
    try:
        from piper import PiperVoice
    except ImportError:
        for py in VENV_PYTHONS:
            if os.path.exists(py) and os.path.realpath(py) != os.path.realpath(sys.executable):
                os.execv(py, [py] + sys.argv)
        sys.exit("Piper kurulu değil. Kurulumda 'Piper' seçeneğini açın ya da: python3 -m venv /opt/orkestra/piper && /opt/orkestra/piper/bin/pip install piper-tts")
    path = ARGS.model
    if os.path.isdir(path):
        found = [os.path.join(d, f) for d, _, fs in os.walk(path) for f in fs if f.endswith(".onnx")]
        if not found:
            sys.exit(f"ses dosyası (.onnx) yok: {path}")
        path = sorted(found)[0]
    os.environ.setdefault("OMP_NUM_THREADS", str(ARGS.threads))
    VOICE = PiperVoice.load(path, config_path=path + ".json")
    RATE = int(VOICE.config.sample_rate)
    GATE = threading.BoundedSemaphore(max(1, ARGS.parallel))
    srv = ThreadingHTTPServer((os.environ.get("ORKESTRA_BIND_HOST") or ARGS.host, ARGS.port), H)
    srv.daemon_threads = True
    print(f"piper hazır: {path} ({RATE} Hz) port {ARGS.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
