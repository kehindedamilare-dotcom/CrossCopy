"""Connected-device registry and fan-out."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from websockets.asyncio.server import ServerConnection

from .protocol import Message

logger = logging.getLogger("crosscopy.hub")


@dataclass(eq=False)
class Peer:
    id: str
    name: str
    platform: str
    connection: ServerConnection
    connected_at: float = field(default_factory=time.time)
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _seen: deque[str] = field(default_factory=lambda: deque(maxlen=256), repr=False)
    _seen_lookup: set[str] = field(default_factory=set, repr=False)

    def seen(self, message_id: str) -> bool:
        """Remember ``message_id``; return False when it is a replay."""
        if message_id in self._seen_lookup:
            return False
        if len(self._seen) == self._seen.maxlen:
            self._seen_lookup.discard(self._seen[0])
        self._seen.append(message_id)
        self._seen_lookup.add(message_id)
        return True

    async def send(self, message: Message) -> None:
        # websockets forbids concurrent writes on one connection.
        async with self._send_lock:
            await self.connection.send(message.to_json())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "platform": self.platform,
            "connected_at": self.connected_at,
        }


class Hub:
    """Holds the live peers. Thread-safe only from the asyncio thread."""

    def __init__(self, server_id: str) -> None:
        self.id = server_id
        self._peers: dict[str, Peer] = {}

    def __len__(self) -> int:
        return len(self._peers)

    def get(self, peer_id: str) -> Peer | None:
        return self._peers.get(peer_id)

    def peers(self) -> list[Peer]:
        return list(self._peers.values())

    async def add(self, peer: Peer) -> Peer | None:
        """Register ``peer`` and return a stale peer that used the same id."""
        previous = self._peers.get(peer.id)
        self._peers[peer.id] = peer
        return previous

    def remove(self, peer: Peer) -> None:
        if self._peers.get(peer.id) is peer:
            del self._peers[peer.id]

    def roster(self) -> list[dict[str, Any]]:
        return [peer.to_dict() for peer in self._peers.values()]

    async def send_to(self, peer_id: str, message: Message) -> bool:
        peer = self._peers.get(peer_id)
        if peer is None:
            return False
        try:
            await peer.send(message)
            return True
        except Exception as exc:  # connection died between lookup and send
            logger.debug("send to %s failed: %s", peer_id, exc)
            return False

    async def broadcast(self, message: Message, exclude: Iterable[str] = ()) -> int:
        skipped = set(exclude)
        targets = [peer for peer in self._peers.values() if peer.id not in skipped]
        if not targets:
            return 0

        results = await asyncio.gather(
            *(peer.send(message) for peer in targets), return_exceptions=True
        )
        delivered = 0
        for peer, result in zip(targets, results):
            if isinstance(result, Exception):
                logger.debug("broadcast to %s failed: %s", peer.id, result)
            else:
                delivered += 1
        return delivered
