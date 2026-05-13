from __future__ import annotations

import json
from collections.abc import AsyncIterator


class OptionalWebSocketClient:
    """Tiny optional wrapper so REST/backtest users do not need websocket deps."""

    def __init__(self, url: str) -> None:
        self.url = url

    async def subscribe(self, channel: str, inst_id: str) -> AsyncIterator[dict]:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("Install optional dependency: pip install websockets") from exc

        async with websockets.connect(self.url, ping_interval=20, ping_timeout=20) as ws:
            await ws.send(json.dumps({"op": "subscribe", "args": [{"channel": channel, "instId": inst_id}]}))
            async for message in ws:
                yield json.loads(message)

