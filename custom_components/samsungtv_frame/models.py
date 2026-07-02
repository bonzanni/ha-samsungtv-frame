"""Data models and pure state derivation for Samsung Frame TV."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TvMode(StrEnum):
    """Derived high-level TV mode used by automations."""

    OFF = "off"
    WATCHING = "watching"
    ART_MODE = "art_mode"
    UNKNOWN = "unknown"


def derive_tv_mode(
    reachable: bool, art_mode: bool | None, power_state: str | None
) -> TvMode:
    """Derive the tri-state from the three raw signals.

    Order matters: unreachable => OFF; art websocket is the source of truth for
    art mode (never gated on PowerState); art-off + powered => WATCHING; a
    reachable TV reporting PowerState "standby" is dark (this Frame model keeps
    its NIC up for several minutes after power-off, answering REST as
    "standby" while reachable) => OFF; anything else is transitional/UNKNOWN
    and is held as last-stable by the coordinator.
    """
    if not reachable:
        return TvMode.OFF
    if art_mode is True:
        return TvMode.ART_MODE
    if art_mode is False and power_state == "on":
        return TvMode.WATCHING
    if power_state == "standby":
        return TvMode.OFF
    return TvMode.UNKNOWN


@dataclass(frozen=True)
class FrameData:
    """Single fan-in snapshot of TV state shared by all entities."""

    reachable: bool
    power_state: str | None
    art_mode: bool | None
    tv_mode: TvMode
    current_art: str | None = None
