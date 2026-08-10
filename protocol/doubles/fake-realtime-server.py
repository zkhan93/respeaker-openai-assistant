#!/usr/bin/env python3
"""A stand-in for OpenAI's Realtime transcription WebSocket.

Speaks just enough of RFC 6455 to check what the Rust core actually put on
the wire: the upgrade handshake, the session configuration, base64 PCM16
appends, and the commit. It answers with deltas and a completion so the
streaming path — partials included — can be exercised end to end.

Stdlib only, on purpose: this runs in CI, where a pip install is a
dependency the harness should not need.
"""
import base64
import hashlib
import json
import socket
import struct
import sys
import threading

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455 §1.3
DELTAS = ["the streaming ", "engine ", "answered"]
report = {"session": None, "appends": 0, "audio_bytes": 0, "commits": 0, "auth": False}


def read_frame(conn):
    """One client frame. Text only, masked, both length forms."""
    header = recv_exact(conn, 2)
    if not header:
        return None
    opcode = header[0] & 0x0F
    masked = header[1] & 0x80
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", recv_exact(conn, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", recv_exact(conn, 8))[0]
    mask = recv_exact(conn, 4) if masked else b"\x00\x00\x00\x00"
    payload = recv_exact(conn, length)
    if payload is None:
        return None
    data = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    if opcode == 0x8:  # close
        return None
    if opcode != 0x1:  # ignore ping/pong/binary
        return b""
    return data


def recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def send_text(conn, obj):
    payload = json.dumps(obj).encode()
    header = bytes([0x81])
    if len(payload) < 126:
        header += bytes([len(payload)])
    else:
        header += bytes([126]) + struct.pack(">H", len(payload))
    conn.sendall(header + payload)


def handshake(conn):
    request = b""
    while b"\r\n\r\n" not in request:
        chunk = conn.recv(4096)
        if not chunk:
            return False
        request += chunk
    headers = {}
    for line in request.decode("latin-1").split("\r\n")[1:]:
        if ": " in line:
            name, _, value = line.partition(": ")
            headers[name.lower()] = value
    report["auth"] = headers.get("authorization", "").startswith("Bearer ")
    accept = accept_for(headers.get("sec-websocket-key", ""))
    conn.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode()
    )
    return True


def serve_client(conn):
    if not handshake(conn):
        return
    while True:
        data = read_frame(conn)
        if data is None:
            break
        if not data:
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue

        kind = event.get("type")
        if kind == "session.update":
            report["session"] = event["session"]
        elif kind == "input_audio_buffer.append":
            report["appends"] += 1
            report["audio_bytes"] += len(base64.b64decode(event["audio"]))
        elif kind == "input_audio_buffer.commit":
            report["commits"] += 1
            # Deltas first, then the final — the order a caption depends on.
            for fragment in DELTAS:
                send_text(
                    conn,
                    {
                        "type": "conversation.item.input_audio_transcription.delta",
                        "delta": fragment,
                    },
                )
            send_text(
                conn,
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "".join(DELTAS),
                },
            )
            print(json.dumps(report), flush=True)
    conn.close()


def accept_for(key):
    return base64.b64encode(hashlib.sha1(key.encode() + GUID).digest()).decode()


if __name__ == "__main__":
    # RFC 6455 §1.3's worked example. Getting the magic GUID subtly wrong
    # fails as "Key mismatch in Sec-WebSocket-Accept" from inside the
    # client, which reads like a bug in the thing under test rather than
    # in the fixture. One assert beats that diagnosis.
    assert accept_for("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="

    port = int(sys.argv[1])
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(4)
    while True:
        conn, _ = server.accept()
        threading.Thread(target=serve_client, args=(conn,), daemon=True).start()
