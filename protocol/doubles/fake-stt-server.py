#!/usr/bin/env python3
"""A stand-in for an OpenAI-compatible transcription server.

Validates what the Rust core actually put on the wire — the multipart
framing, the WAV container, the declared sample rate — then answers with a
fixed transcript. That makes the remote engine testable without a network,
an API key, or a GPU box.
"""
import io
import json
import re
import sys
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer

TRANSCRIPT = "the remote engine answered"
seen = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # stdout is for the report

    def do_POST(self):
        if not self.path.endswith("/audio/transcriptions"):
            self.send_error(404)
            return

        ctype = self.headers.get("Content-Type", "")
        match = re.search(r"boundary=([^\s;]+)", ctype)
        if not match:
            self.send_error(400, "no multipart boundary")
            return
        boundary = match.group(1).encode()
        body = self.rfile.read(int(self.headers["Content-Length"]))

        parts = body.split(b"--" + boundary)
        fields, wav_bytes = {}, None
        for part in parts:
            if not part.strip(b"-\r\n"):
                continue
            head, _, payload = part.partition(b"\r\n\r\n")
            payload = payload.rstrip(b"\r\n")
            name = re.search(rb'name="([^"]+)"', head)
            if not name:
                continue
            if b"filename=" in head:
                wav_bytes = payload
            else:
                fields[name.group(1).decode()] = payload.decode()

        report = {
            "auth": "yes" if self.headers.get("Authorization") else "no",
            "fields": fields,
            "wav_bytes": len(wav_bytes) if wav_bytes else 0,
        }
        if wav_bytes:
            report["riff"] = wav_bytes[:4] == b"RIFF"
            try:
                with wave.open(io.BytesIO(wav_bytes)) as w:
                    report["rate"] = w.getframerate()
                    report["channels"] = w.getnchannels()
                    report["width"] = w.getsampwidth()
                    report["seconds"] = round(w.getnframes() / w.getframerate(), 2)
            except Exception as exc:
                report["wav_error"] = str(exc)
        seen.append(report)
        print(json.dumps(report), flush=True)

        payload = json.dumps({"text": TRANSCRIPT}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    port = int(sys.argv[1])
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
