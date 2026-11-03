"""Async Source-RCON client for Factorio (based on a small stdlib-only host-side helper).

Keeps one persistent connection so the game log isn't flooded with
"New RCON connection" lines, and reconnects transparently once on failure.
Factorio answers each command with a single packet carrying the same id and
rejects SERVERDATA_RESPONSE_VALUE (type 0) packets, so no multi-packet trick.
"""
import asyncio
import struct

AUTH, EXEC = 3, 2


def _packet(req_id: int, ptype: int, body: str) -> bytes:
    data = struct.pack("<ii", req_id, ptype) + body.encode() + b"\x00\x00"
    return struct.pack("<i", len(data)) + data


class RconError(Exception):
    pass


class Rcon:
    def __init__(self, host: str, port: int, password_file: str, timeout: float = 15):
        self.host, self.port, self.password_file, self.timeout = host, port, password_file, timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._next_id = 10

    async def _read_packet(self) -> tuple[int, int, str]:
        assert self._reader
        (size,) = struct.unpack("<i", await self._reader.readexactly(4))
        data = await self._reader.readexactly(size)
        req_id, ptype = struct.unpack("<ii", data[:8])
        return req_id, ptype, data[8:-2].decode(errors="replace")

    async def _connect(self) -> None:
        password = open(self.password_file).read().strip()
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), self.timeout)
        self._writer.write(_packet(1, AUTH, password))
        await self._writer.drain()
        req_id, _, _ = await asyncio.wait_for(self._read_packet(), self.timeout)
        if req_id == -1:
            await self.close()
            raise RconError("RCON authentication failed")

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = self._writer = None

    async def _send(self, command: str) -> str:
        if not self._writer:
            await self._connect()
        self._next_id = self._next_id % 1_000_000 + 1
        rid = self._next_id
        self._writer.write(_packet(rid, EXEC, command))
        await self._writer.drain()
        while True:
            req_id, _, body = await asyncio.wait_for(self._read_packet(), self.timeout)
            if req_id == rid:
                return body.rstrip()

    async def command(self, command: str) -> str:
        """Run one console command; reconnect and retry once on a broken connection."""
        async with self._lock:
            try:
                return await self._send(command)
            except RconError:
                raise
            except Exception:
                await self.close()
                try:
                    return await self._send(command)
                except RconError:
                    raise
                except Exception as e:
                    await self.close()
                    raise RconError(f"RCON unreachable ({type(e).__name__}): is the server running?") from e

    async def ping(self) -> bool:
        try:
            await self.command("/version")
            return True
        except Exception:
            return False
