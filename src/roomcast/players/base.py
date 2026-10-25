"""What every speaker backend offers the MCP tools.

rooms() maps each name to a backend-specific handle, which the other methods take, so a call
does not repeat discovery. Positions are 1-based, as the tools show them."""
from dataclasses import dataclass
from typing import Any, Literal, Protocol

# "started": playing now. "waiting": queued behind what plays. "elsewhere": queued, but the room
# plays another source, so it has not started. "busy": nothing queued, because another app owns
# the speaker and its queue cannot be added to.
Outcome = Literal["started", "waiting", "elsewhere", "busy"]


@dataclass
class MediaItem:
    url: str
    title: str
    subtitle: str | None = None
    media_type: str | None = None

    @property
    def label(self) -> str:
        return f"{self.subtitle}: {self.title}" if self.subtitle else self.title


def parse_hosts(value: str) -> dict[str, str]:
    """`Kitchen=192.168.1.20,Living Room=192.168.1.21` -> {"kitchen": "192.168.1.20", ...}."""
    hosts = {}
    for pair in filter(None, (p.strip() for p in value.split(","))):
        name, _, ip = pair.partition("=")
        if ip:
            hosts[name.strip().lower()] = ip.strip()
    return hosts


def parse_list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def check_positions(positions: list[int], size: int) -> None:
    bad = [p for p in positions if not 1 <= p <= size]
    if bad:
        raise ValueError(f"No queue position {', '.join(map(str, bad))}; the queue has {size} items")


class Player(Protocol):
    kind: str

    async def rooms(self) -> dict[str, Any]: ...

    async def play(self, room: Any, items: list[MediaItem], volume: int | None) -> str: ...

    async def append(self, room: Any, items: list[MediaItem],
                     after_current: bool = False) -> tuple[str, int, Outcome]:
        """after_current inserts right after what is playing, but only when the outcome is
        "waiting"; otherwise it behaves exactly like adding to the end."""
        ...

    async def queue(self, room: Any) -> str: ...

    async def remove(self, room: Any, positions: list[int]) -> str: ...

    async def jump(self, room: Any, position: int) -> str: ...

    async def stop(self, room: Any) -> str: ...

    async def next(self, room: Any) -> str: ...
