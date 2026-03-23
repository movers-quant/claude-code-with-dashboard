"""Minimal RFC 6455 WebSocket implementation using asyncio."""

import asyncio
import base64
import hashlib
import json
import struct
from typing import Optional

WS_MAGIC = "258EAFA5-E914-47DA-95CA-5AB9D6E37964"

# Connected clients
clients: set["WebSocketConnection"] = set()


class WebSocketConnection:
    """Minimal WebSocket connection over an asyncio StreamReader/Writer."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self._closed = False

    async def send(self, text: str) -> None:
        if self._closed:
            return
        data = text.encode()
        header = bytearray()
        header.append(0x81)  # FIN + text opcode
        length = len(data)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(127)
            header.extend(struct.pack("!Q", length))
        try:
            self.writer.write(bytes(header) + data)
            await self.writer.drain()
        except Exception:
            self._closed = True

    async def recv(self) -> Optional[str]:
        """Read one WebSocket text frame. Returns None on close/error."""
        try:
            b0 = await self.reader.readexactly(1)
            b1 = await self.reader.readexactly(1)
            opcode = b0[0] & 0x0F
            masked = b1[0] & 0x80
            length = b1[0] & 0x7F

            if length == 126:
                raw = await self.reader.readexactly(2)
                length = struct.unpack("!H", raw)[0]
            elif length == 127:
                raw = await self.reader.readexactly(8)
                length = struct.unpack("!Q", raw)[0]

            mask_key = await self.reader.readexactly(4) if masked else None

            payload = await self.reader.readexactly(length)
            if mask_key:
                payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

            if opcode == 0x8:  # close
                return None
            if opcode == 0x9:  # ping → pong
                await self._send_pong(payload)
                return await self.recv()
            return payload.decode(errors="replace")
        except Exception:
            return None

    async def _send_pong(self, data: bytes) -> None:
        header = bytearray([0x8A, len(data)])
        try:
            self.writer.write(bytes(header) + data)
            await self.writer.drain()
        except Exception:
            self._closed = True

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self.writer.write(bytes([0x88, 0x00]))
                await self.writer.drain()
                self.writer.close()
            except Exception:
                pass

    @property
    def open(self) -> bool:
        return not self._closed


def compute_accept_key(key: str) -> str:
    """Compute Sec-WebSocket-Accept from client key."""
    digest = hashlib.sha1((key.strip() + WS_MAGIC).encode()).digest()
    return base64.b64encode(digest).decode()


async def broadcast(msg: dict) -> None:
    """Send a JSON message to all connected WebSocket clients."""
    data = json.dumps(msg)
    dead: list[WebSocketConnection] = []
    for ws in clients:
        if ws.open:
            await ws.send(data)
        else:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)
