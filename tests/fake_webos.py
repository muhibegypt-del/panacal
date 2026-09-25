"""A minimal webOS SSAP endpoint on wss://127.0.0.1:3001 for transport tests.

It speaks just enough of the LG protocol for pgenerator-lg's probe, connect
and PIN pairing: hello, getSystemInfo, getCurrentSWInformation, register and
pairing/setPin. After a PIN it sends the setPin response and the
"registered" message in ONE TLS write, so the client must read the second
message out of the TLS buffer (the case the PC-PORT pending() patch covers).
"""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import ssl
import struct
import subprocess
import threading
from pathlib import Path

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PIN = "12345678"
KEY = "fake-client-key"


def make_certificate(directory: Path) -> tuple[Path, Path]:
    cert, key = directory / "tv.crt", directory / "tv.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=lgtv",
                    "-days", "1", "-keyout", str(key), "-out", str(cert)],
                   check=True, capture_output=True)
    return cert, key


def frame(payload: dict) -> bytes:
    data = json.dumps(payload).encode()
    header = bytes([0x81])
    if len(data) < 126:
        header += bytes([len(data)])
    elif len(data) < 65536:
        header += bytes([126]) + struct.pack(">H", len(data))
    else:
        header += bytes([127]) + struct.pack(">Q", len(data))
    return header + data


def read_exact(conn, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = conn.recv(count - len(data))
        if not chunk:
            raise ConnectionError
        data += chunk
    return data


def read_frame(conn) -> tuple[int, bytes]:
    first, second = read_exact(conn, 2)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack(">H", read_exact(conn, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", read_exact(conn, 8))[0]
    mask = read_exact(conn, 4) if second & 0x80 else b"\0\0\0\0"
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(read_exact(conn, length)))
    return first & 0x0F, payload


class FakeWebOS:
    def __init__(self, directory: Path, port: int = 3001):
        cert, key = make_certificate(directory)
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen()
        self.log: list[str] = []
        self.running = True
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self.running = False
        self.sock.close()

    def _accept(self) -> None:
        while self.running:
            try:
                raw, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(raw,), daemon=True).start()

    def _serve(self, raw) -> None:
        with raw:
            self._session(raw)

    def _session(self, raw) -> None:
        try:
            with self.context.wrap_socket(raw, server_side=True) as conn:
                self._converse(conn)
        except (ConnectionError, OSError, ssl.SSLError, IndexError):
            return

    def _converse(self, conn) -> None:
        request = b""
        while b"\r\n\r\n" not in request:
            request += conn.recv(1024)
        key = [line.split(b":", 1)[1].strip() for line in request.split(b"\r\n")
               if line.lower().startswith(b"sec-websocket-key")][0]
        accept = base64.b64encode(hashlib.sha1(key + GUID.encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        while True:
            opcode, payload = read_frame(conn)
            if opcode == 0x8:
                return
            if opcode == 0x9:
                conn.sendall(bytes([0x8A, len(payload)]) + payload)
                continue
            self._message(conn, json.loads(payload))

    def _message(self, conn, message: dict) -> None:
        kind, uri, mid = message.get("type"), message.get("uri", ""), message.get("id")
        self.log.append(kind if kind != "request" else uri)
        if kind == "hello":
            conn.sendall(frame({"type": "hello", "id": mid, "payload": {
                "deviceOS": "webOS", "deviceType": "tv", "deviceOSReleaseVersion": "6.0.0",
                "deviceUUID": "fake-uuid", "pairingTypes": ["PIN", "PROMPT"]}}))
        elif kind == "register":
            payload = message.get("payload") or {}
            if payload.get("client-key") == KEY:
                conn.sendall(frame({"type": "registered", "id": mid, "payload": {"client-key": KEY}}))
            else:
                conn.sendall(frame({"type": "response", "id": mid, "payload": {
                    "pairingType": payload.get("pairingType", "PROMPT"), "returnValue": True}}))
        elif uri == "ssap://pairing/setPin":
            if (message.get("payload") or {}).get("pin") == PIN:
                both = (frame({"type": "response", "id": mid, "payload": {"returnValue": True}})
                        + frame({"type": "registered", "id": "register_0", "payload": {"client-key": KEY}}))
                conn.sendall(both)
            else:
                conn.sendall(frame({"type": "error", "id": mid, "error": "401 wrong PIN"}))
        elif uri == "ssap://system/getSystemInfo":
            conn.sendall(frame({"type": "response", "id": mid, "payload": {
                "returnValue": True, "modelName": "OLED65C1FAKE", "features": {}}}))
        elif uri == "ssap://com.webos.service.update/getCurrentSWInformation":
            conn.sendall(frame({"type": "response", "id": mid, "payload": {
                "returnValue": True, "product_name": "webOSTV 6.0", "model_name": "HE_DTV_W21O_AFABATAA",
                "major_ver": "03", "minor_ver": "20.00", "device_id": "aa:bb:cc:dd:ee:ff"}}))
        else:
            conn.sendall(frame({"type": "error", "id": mid, "error": "404 no such service or method"}))
