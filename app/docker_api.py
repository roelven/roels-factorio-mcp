"""Minimal Docker Engine API client, hard-wired to ONE container: `factorio`.

The host may run other services. This module deliberately exposes
only three operations (inspect, logs, restart) and only for the container
named in TARGET. There is no list/start/stop/exec/pull, and the name is
not a parameter anywhere. Do not add one.
"""
import struct
from datetime import datetime, timezone

import httpx

TARGET = "factorio"  # hard-coded on purpose; never make this configurable
_BASE = f"http://docker/containers/{TARGET}"


class DockerApi:
    def __init__(self, socket_path: str):
        self._client = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=socket_path), timeout=30)

    async def inspect(self) -> dict:
        r = await self._client.get(f"{_BASE}/json")
        r.raise_for_status()
        return r.json()

    async def state(self) -> dict:
        info = await self.inspect()
        st = info["State"]
        started = datetime.fromisoformat(st["StartedAt"][:26].rstrip("Z") + "+00:00")
        up = datetime.now(timezone.utc) - started
        h, rem = divmod(int(up.total_seconds()), 3600)
        return {
            "name": info["Name"].lstrip("/"),
            "status": st["Status"],
            "health": (st.get("Health") or {}).get("Status", "none"),
            "started_at": st["StartedAt"][:19] + "Z",
            "uptime": f"{h // 24}d {h % 24}h {rem // 60}m" if st["Running"] else "-",
            "restart_count": info.get("RestartCount", 0),
            "image": info["Config"]["Image"],
        }

    async def logs(self, tail: int = 200, since: int | None = None) -> list[str]:
        """Log lines, each prefixed with Docker's RFC3339 timestamp and a space."""
        params = {"stdout": 1, "stderr": 1, "timestamps": 1, "tail": str(tail)}
        if since:
            params["since"] = str(since)
        r = await self._client.get(f"{_BASE}/logs", params=params)
        r.raise_for_status()
        return _demux(r.content).splitlines()

    async def restart(self, stop_timeout: int = 60) -> None:
        r = await self._client.post(f"{_BASE}/restart", params={"t": str(stop_timeout)},
                                    timeout=stop_timeout + 60)
        r.raise_for_status()


def _demux(raw: bytes) -> str:
    """Strip Docker's 8-byte stream headers (non-TTY containers)."""
    out, i = [], 0
    while i + 8 <= len(raw) and raw[i] in (0, 1, 2) and raw[i + 1:i + 4] == b"\0\0\0":
        (n,) = struct.unpack(">I", raw[i + 4:i + 8])
        out.append(raw[i + 8:i + 8 + n])
        i += 8 + n
    if i == 0:
        return raw.decode(errors="replace")
    return b"".join(out).decode(errors="replace")
