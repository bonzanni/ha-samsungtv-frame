# Samsung Frame TV — P1a Core Accurate State: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a working HA custom integration that exposes an accurate, push-driven
OFF / WATCHING / ART tri-state for a Samsung Frame TV, plus power on/off and art-mode toggle.

**Architecture:** Standalone integration on `samsungtvws` 3.0.5 (local network). One config entry
per TV. A `FrameCoordinator` (in `entry.runtime_data`) fans three signals into one `FrameData`
dataclass: async REST `PowerState`+reachability heartbeat (~10s), async remote-channel liveness,
and **push** art-mode events from the sync `SamsungTVArt.start_listening` thread bridged to the
event loop. A pure `derive_tv_mode()` function turns those signals into the tri-state. Three
entities (`media_player`, `binary_sensor`, ENUM `sensor`) read the shared `FrameData`.

**Tech Stack:** Python 3.13+, Home Assistant custom integration, `samsungtvws[async,encrypted]==3.0.5`,
`wakeonlan==3.3.0`, `pytest` + `pytest-homeassistant-custom-component`.

## Global Constraints

- Full design spec: `docs/superpowers/specs/2026-07-01-samsung-frame-tv-design.md`. This plan
  implements the **P1a MVP subset** (state + power + art toggle). Source/volume/playback,
  `send_key`, options/reconfigure = P1b; art management = P2.
- Domain: `samsungtv_frame`. Display name: `Samsung Frame TV`. Repo: `bonzanni/ha-samsungtv-frame`.
- Manifest: `integration_type: "device"`, `iot_class: "local_push"`, `quality_scale: "silver"`,
  `version` present, `codeowners: ["@bonzanni"]`,
  `documentation: "https://github.com/bonzanni/ha-samsungtv-frame"`,
  `issue_tracker: "https://github.com/bonzanni/ha-samsungtv-frame/issues"`,
  `loggers: ["samsungtvws"]`.
- **Fixed websocket client name = `"Home Assistant"`** everywhere (pairing + runtime). The TV's
  token grant is keyed to this name; a different name re-prompts. Never change it.
- State derivation (verified on QE65LS03B): `not reachable → OFF`; `art_mode is True → ART_MODE`
  (do NOT gate on PowerState); `art_mode is False and PowerState=="on" → WATCHING`; else UNKNOWN
  (held as last-stable, never surfaced as a flapping state).
- Turn-on from OFF = **Wake-on-LAN** (no socket answers when off). Turn-off = **`KEY_POWER` held
  3 s** (single press only toggles art mode).
- All TV I/O is non-blocking: async remote/REST used directly; sync `SamsungTVArt` calls wrapped
  in `hass.async_add_executor_job`; its `start_listening` thread callback bridged with
  `hass.loop.call_soon_threadsafe`.
- `PARALLEL_UPDATES = 0` in every platform module (coordinator-fed).
- TDD: every task writes the failing test first. Commit after every green task.

---

### Task 1: Project scaffold, manifest, constants, test harness

**Files:**
- Create: `custom_components/samsungtv_frame/__init__.py` (minimal, expanded in Task 6)
- Create: `custom_components/samsungtv_frame/manifest.json`
- Create: `custom_components/samsungtv_frame/const.py`
- Create: `hacs.json`
- Create: `tests/__init__.py`, `tests/conftest.py`
- Create: `requirements_test.txt`
- Create: `pyproject.toml` (pytest config + tool settings)

**Interfaces:**
- Produces: `DOMAIN = "samsungtv_frame"`, `PLATFORMS`, config keys `CONF_HOST`/`CONF_MAC`/`CONF_TOKEN`,
  `CLIENT_NAME = "Home Assistant"`, `PORT_REST = 8001`, `PORT_WS = 8002`,
  `DEFAULT_HEARTBEAT = timedelta(seconds=10)`, `LOGGER`.

- [ ] **Step 1: Write `const.py`**

```python
"""Constants for the Samsung Frame TV integration."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "samsungtv_frame"
LOGGER = logging.getLogger(__package__)

PLATFORMS: list[Platform] = [
    Platform.MEDIA_PLAYER,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
]

# Config entry keys
CONF_HOST = "host"
CONF_MAC = "mac"
CONF_TOKEN = "token"
CONF_MODEL = "model"

# Fixed websocket client name — the TV's token grant is keyed to this. Never change.
CLIENT_NAME = "Home Assistant"

PORT_REST = 8001
PORT_WS = 8002

DEFAULT_HEARTBEAT = timedelta(seconds=10)
# Consecutive unreachable heartbeats before declaring OFF (debounce transient drops).
OFF_DEBOUNCE_COUNT = 2
```

- [ ] **Step 2: Write `manifest.json`**

```json
{
  "domain": "samsungtv_frame",
  "name": "Samsung Frame TV",
  "version": "0.1.0",
  "integration_type": "device",
  "iot_class": "local_push",
  "quality_scale": "silver",
  "config_flow": true,
  "codeowners": ["@bonzanni"],
  "documentation": "https://github.com/bonzanni/ha-samsungtv-frame",
  "issue_tracker": "https://github.com/bonzanni/ha-samsungtv-frame/issues",
  "loggers": ["samsungtvws"],
  "requirements": ["samsungtvws[async,encrypted]==3.0.5", "wakeonlan==3.3.0"],
  "dependencies": [],
  "ssdp": [
    {"manufacturer": "Samsung Electronics", "deviceType": "urn:samsung.com:device:RemoteControlReceiver:1"}
  ],
  "dhcp": [
    {"hostname": "samsung*"}
  ]
}
```

- [ ] **Step 3: Write `hacs.json`**

```json
{
  "name": "Samsung Frame TV",
  "homeassistant": "2026.1.0",
  "render_readme": true
}
```

- [ ] **Step 4: Write minimal `__init__.py`** (expanded in Task 6)

```python
"""The Samsung Frame TV integration."""
from __future__ import annotations
```

- [ ] **Step 5: Write `tests/conftest.py`**

```python
"""Shared fixtures for Samsung Frame TV tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of the custom integration in every test."""
    yield


@pytest.fixture
def mock_device() -> MagicMock:
    """A mocked FrameDevice with async methods."""
    device = MagicMock()
    device.async_device_info = AsyncMock(
        return_value={"PowerState": "on", "FrameTVSupport": "true",
                      "wifiMac": "A0:D0:5B:86:CE:B7", "modelName": "QE65LS03BAUXXH"}
    )
    device.async_get_artmode = AsyncMock(return_value=False)
    device.async_set_artmode = AsyncMock()
    device.async_turn_on = AsyncMock()
    device.async_turn_off = AsyncMock()
    device.async_start_art_listener = AsyncMock()
    device.async_stop = AsyncMock()
    return device
```

- [ ] **Step 6: Write `requirements_test.txt`**

```
pytest-homeassistant-custom-component
samsungtvws[async,encrypted]==3.0.5
wakeonlan==3.3.0
```

- [ ] **Step 7: Write `pyproject.toml`**

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
```

- [ ] **Step 8: Install test deps and verify collection**

Run: `.venv/bin/pip install -r requirements_test.txt && .venv/bin/python -m pytest -q`
Expected: `no tests ran` (0 collected) with exit 0 — harness imports cleanly.

- [ ] **Step 9: Commit**

```bash
git add custom_components hacs.json tests requirements_test.txt pyproject.toml
git commit -m "feat: scaffold samsungtv_frame integration and test harness"
```

---

### Task 2: TvMode enum, FrameData dataclass, and pure state derivation

This is the **core value** of the whole integration — a pure function, exhaustively unit-tested.

**Files:**
- Create: `custom_components/samsungtv_frame/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces:
  - `class TvMode(StrEnum)` with `OFF="off"`, `WATCHING="watching"`, `ART_MODE="art_mode"`, `UNKNOWN="unknown"`.
  - `@dataclass(frozen=True) class FrameData` fields: `reachable: bool`, `power_state: str | None`,
    `art_mode: bool | None`, `tv_mode: TvMode`, `current_art: str | None`.
  - `def derive_tv_mode(reachable: bool, art_mode: bool | None, power_state: str | None) -> TvMode`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models.py
import pytest

from custom_components.samsungtv_frame.models import TvMode, derive_tv_mode


@pytest.mark.parametrize(
    ("reachable", "art_mode", "power_state", "expected"),
    [
        (False, None, None, TvMode.OFF),          # unreachable => OFF regardless
        (False, True, "on", TvMode.OFF),          # unreachable wins even if art cached True
        (True, True, "on", TvMode.ART_MODE),      # art is source of truth
        (True, True, "standby", TvMode.ART_MODE), # do NOT gate art on PowerState (#185 trap)
        (True, False, "on", TvMode.WATCHING),     # art off + powered => watching
        (True, False, "standby", TvMode.UNKNOWN), # reachable but powered-off-ish => transitional
        (True, None, "on", TvMode.UNKNOWN),       # art unknown yet => transitional
    ],
)
def test_derive_tv_mode(reachable, art_mode, power_state, expected):
    assert derive_tv_mode(reachable, art_mode, power_state) == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError`/`ImportError` for `models`.

- [ ] **Step 3: Write `models.py`**

```python
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
    art mode (never gated on PowerState); art-off + powered => WATCHING; anything
    else is transitional/UNKNOWN and is held as last-stable by the coordinator.
    """
    if not reachable:
        return TvMode.OFF
    if art_mode is True:
        return TvMode.ART_MODE
    if art_mode is False and power_state == "on":
        return TvMode.WATCHING
    return TvMode.UNKNOWN


@dataclass(frozen=True)
class FrameData:
    """Single fan-in snapshot of TV state shared by all entities."""

    reachable: bool
    power_state: str | None
    art_mode: bool | None
    tv_mode: TvMode
    current_art: str | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_models.py -v`
Expected: PASS (7 parametrized cases).

- [ ] **Step 5: Commit**

```bash
git add custom_components/samsungtv_frame/models.py tests/test_models.py
git commit -m "feat: add TvMode, FrameData, and pure state derivation"
```

---

### Task 3: FrameDevice async facade over samsungtvws

Wraps the library so the coordinator sees a clean async surface. Async remote/REST used directly;
sync art wrapped in the executor; WoL for power-on; 3 s `KEY_POWER` hold for power-off.

**Files:**
- Create: `custom_components/samsungtv_frame/device.py`
- Test: `tests/test_device.py`

**Interfaces:**
- Consumes: constants from `const.py`.
- Produces `class FrameDevice`:
  - `__init__(self, hass, host: str, mac: str, token: str | None)`
  - `async def async_device_info(self) -> dict | None` — REST `device` dict or `None` if unreachable.
  - `async def async_get_artmode(self) -> bool | None` — executor-wrapped `get_artmode()=="on"`.
  - `async def async_set_artmode(self, on: bool) -> None`
  - `async def async_turn_on(self) -> None` — WoL magic packet.
  - `async def async_turn_off(self) -> None` — `KEY_POWER` held 3 s via async remote.
  - `async def async_start_art_listener(self, callback: Callable[[str, Any], None]) -> None`
  - `async def async_stop(self) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_device.py
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.samsungtv_frame.device import FrameDevice


@pytest.fixture
def device(hass):
    return FrameDevice(hass, host="1.2.3.4", mac="A0:D0:5B:86:CE:B7", token="tok")


async def test_device_info_returns_device_dict(hass, device):
    rest = MagicMock()
    rest.rest_device_info = AsyncMock(return_value={"device": {"PowerState": "on"}})
    with patch.object(device, "_rest", rest):
        info = await device.async_device_info()
    assert info == {"PowerState": "on"}


async def test_device_info_none_when_unreachable(hass, device):
    rest = MagicMock()
    rest.rest_device_info = AsyncMock(side_effect=OSError("timeout"))
    with patch.object(device, "_rest", rest):
        assert await device.async_device_info() is None


async def test_get_artmode_true(hass, device):
    art = MagicMock()
    art.get_artmode.return_value = "on"
    with patch.object(device, "_art", art):
        assert await device.async_get_artmode() is True


async def test_turn_on_sends_magic_packet(hass, device):
    with patch("custom_components.samsungtv_frame.device.send_magic_packet") as smp:
        await device.async_turn_on()
    smp.assert_called_once()
    assert smp.call_args.args[0] == "A0:D0:5B:86:CE:B7"


async def test_turn_off_holds_power_key(hass, device):
    remote = MagicMock()
    remote.send_commands = AsyncMock()
    with patch.object(device, "_remote", remote):
        await device.async_turn_off()
    remote.send_commands.assert_awaited_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_device.py -v`
Expected: FAIL with `ImportError` for `device`.

- [ ] **Step 3: Write `device.py`**

```python
"""Async facade over the samsungtvws library for a Frame TV."""
from __future__ import annotations

from typing import Any, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from samsungtvws.art import SamsungTVArt
from samsungtvws.async_remote import SamsungTVWSAsyncRemote
from samsungtvws.async_rest import SamsungTVAsyncRest
from samsungtvws.remote import SendRemoteKey
from wakeonlan import send_magic_packet

from .const import CLIENT_NAME, LOGGER, PORT_REST, PORT_WS


class FrameDevice:
    """Clean async surface the coordinator talks to."""

    def __init__(
        self, hass: HomeAssistant, host: str, mac: str, token: str | None
    ) -> None:
        self._hass = hass
        self._host = host
        self._mac = mac
        self._token = token
        session = async_get_clientsession(hass)
        self._rest = SamsungTVAsyncRest(host, session=session, port=PORT_REST, timeout=8)
        self._remote = SamsungTVWSAsyncRemote(
            host, token=token, port=PORT_WS, name=CLIENT_NAME, timeout=8
        )
        # Sync art client — its calls are executor-wrapped; its listener runs its own thread.
        self._art = SamsungTVArt(
            host, token=token, port=PORT_WS, name=CLIENT_NAME, timeout=8
        )

    async def async_device_info(self) -> dict[str, Any] | None:
        try:
            info = await self._rest.rest_device_info()
        except Exception as err:  # noqa: BLE001 - library raises broad connection types
            LOGGER.debug("REST device info failed for %s: %s", self._host, err)
            return None
        return info.get("device") if info else None

    async def async_get_artmode(self) -> bool | None:
        try:
            value = await self._hass.async_add_executor_job(self._art.get_artmode)
        except Exception as err:  # noqa: BLE001
            LOGGER.debug("get_artmode failed: %s", err)
            return None
        return value == "on"

    async def async_set_artmode(self, on: bool) -> None:
        await self._hass.async_add_executor_job(self._art.set_artmode, on)

    async def async_turn_on(self) -> None:
        await self._hass.async_add_executor_job(
            lambda: send_magic_packet(self._mac, ip_address="255.255.255.255")
        )

    async def async_turn_off(self) -> None:
        # Single press only toggles art mode; a 3 s hold truly powers a Frame off.
        await self._remote.send_commands(SendRemoteKey.hold("KEY_POWER", 3))

    async def async_start_art_listener(
        self, callback: Callable[[str, Any], None]
    ) -> None:
        await self._hass.async_add_executor_job(self._art.start_listening, callback)

    async def async_stop(self) -> None:
        try:
            await self._remote.close()
        except Exception:  # noqa: BLE001
            pass
        await self._hass.async_add_executor_job(self._art.close)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_device.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add custom_components/samsungtv_frame/device.py tests/test_device.py
git commit -m "feat: add FrameDevice async facade over samsungtvws"
```

---

### Task 4: FrameCoordinator — heartbeat poll, derivation, OFF debounce

**Files:**
- Create: `custom_components/samsungtv_frame/coordinator.py`
- Test: `tests/test_coordinator.py`

**Interfaces:**
- Consumes: `FrameDevice` (Task 3), `FrameData`/`TvMode`/`derive_tv_mode` (Task 2), constants.
- Produces:
  - `type FrameConfigEntry = ConfigEntry[FrameCoordinator]`
  - `class FrameCoordinator(DataUpdateCoordinator[FrameData])` with `__init__(hass, entry, device)`,
    `async def _async_update_data() -> FrameData`, and `def handle_art_event(event: str, data: Any) -> None`
    (loop-safe entry point that recomputes and pushes new data).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_coordinator.py
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.config_entries import ConfigEntry

from custom_components.samsungtv_frame.coordinator import FrameCoordinator
from custom_components.samsungtv_frame.models import TvMode


def _make(hass, device) -> FrameCoordinator:
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "abc"
    return FrameCoordinator(hass, entry, device)


async def test_update_watching(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    coord = _make(hass, mock_device)
    data = await coord._async_update_data()
    assert data.tv_mode is TvMode.WATCHING


async def test_update_art_mode(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = True
    coord = _make(hass, mock_device)
    data = await coord._async_update_data()
    assert data.tv_mode is TvMode.ART_MODE


async def test_off_requires_debounce(hass, mock_device):
    mock_device.async_device_info.return_value = None  # unreachable
    coord = _make(hass, mock_device)
    coord.data = MagicMock(tv_mode=TvMode.WATCHING)  # last stable
    # First unreachable poll -> hold last stable, not OFF yet
    first = await coord._async_update_data()
    assert first.tv_mode is TvMode.WATCHING
    # Second consecutive unreachable -> OFF
    second = await coord._async_update_data()
    assert second.tv_mode is TvMode.OFF
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_coordinator.py -v`
Expected: FAIL with `ImportError` for `coordinator`.

- [ ] **Step 3: Write `coordinator.py`**

```python
"""Data update coordinator for a Samsung Frame TV."""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DEFAULT_HEARTBEAT, DOMAIN, LOGGER, OFF_DEBOUNCE_COUNT
from .device import FrameDevice
from .models import FrameData, TvMode, derive_tv_mode

type FrameConfigEntry = ConfigEntry[FrameCoordinator]


class FrameCoordinator(DataUpdateCoordinator[FrameData]):
    """Fan REST + art signals into one FrameData, with OFF debounce."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, device: FrameDevice
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=DEFAULT_HEARTBEAT,
            config_entry=entry,
            always_update=False,
        )
        self.device = device
        self._unreachable_count = 0
        self._art_mode: bool | None = None

    def _last_stable(self) -> TvMode:
        if self.data is not None and self.data.tv_mode is not TvMode.UNKNOWN:
            return self.data.tv_mode
        return TvMode.UNKNOWN

    async def _async_update_data(self) -> FrameData:
        info = await self.device.async_device_info()
        reachable = info is not None
        power_state = info.get("PowerState") if info else None
        current_art: str | None = None

        if reachable:
            self._unreachable_count = 0
            self._art_mode = await self.device.async_get_artmode()
        else:
            self._unreachable_count += 1

        # OFF debounce: only declare OFF after N consecutive unreachable polls.
        if not reachable and self._unreachable_count < OFF_DEBOUNCE_COUNT:
            mode = self._last_stable()
        else:
            mode = derive_tv_mode(reachable, self._art_mode, power_state)
            if mode is TvMode.UNKNOWN:
                mode = self._last_stable()

        return FrameData(
            reachable=reachable,
            power_state=power_state,
            art_mode=self._art_mode,
            tv_mode=mode,
            current_art=current_art,
        )

    @callback
    def handle_art_event(self, event: str, data: Any) -> None:
        """Loop-safe handler for pushed art events (see Task 5 bridge)."""
        sub = data.get("event") if isinstance(data, dict) else None
        if sub in ("art_mode_changed", "artmode_status"):
            value = data.get("value") or data.get("status")
            self._art_mode = value == "on"
        elif sub == "go_to_standby":
            self._art_mode = False
        else:
            return
        mode = derive_tv_mode(True, self._art_mode, "on")
        if mode is TvMode.UNKNOWN:
            mode = self._last_stable()
        current = self.data
        self.async_set_updated_data(
            FrameData(
                reachable=True,
                power_state=current.power_state if current else "on",
                art_mode=self._art_mode,
                tv_mode=mode,
                current_art=current.current_art if current else None,
            )
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_coordinator.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add custom_components/samsungtv_frame/coordinator.py tests/test_coordinator.py
git commit -m "feat: add FrameCoordinator with heartbeat poll and OFF debounce"
```

---

### Task 5: Art push bridge — thread callback → loop → coordinator

Upgrades art-mode latency from poll (~10 s) to sub-second by feeding
`SamsungTVArt.start_listening` events into `coordinator.handle_art_event` on the loop thread.

**Files:**
- Create: `custom_components/samsungtv_frame/art_listener.py`
- Test: `tests/test_art_listener.py`

**Interfaces:**
- Consumes: `FrameCoordinator.handle_art_event` (Task 4), `FrameDevice.async_start_art_listener` (Task 3).
- Produces: `def make_art_bridge(hass, coordinator) -> Callable[[str, Any], None]` — returns a
  thread-safe callback that marshals events onto the loop via `hass.loop.call_soon_threadsafe`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_art_listener.py
from unittest.mock import MagicMock

from custom_components.samsungtv_frame.art_listener import make_art_bridge


async def test_bridge_marshals_event_to_loop(hass):
    coordinator = MagicMock()
    bridge = make_art_bridge(hass, coordinator)
    # Called from a non-loop thread in reality; here we just assert it schedules.
    bridge("d2d_service_message", {"event": "art_mode_changed", "value": "on"})
    await hass.async_block_till_done()
    coordinator.handle_art_event.assert_called_once_with(
        "d2d_service_message", {"event": "art_mode_changed", "value": "on"}
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_art_listener.py -v`
Expected: FAIL with `ImportError` for `art_listener`.

- [ ] **Step 3: Write `art_listener.py`**

```python
"""Bridge the sync art-listener thread onto the HA event loop."""
from __future__ import annotations

from typing import Any, Callable

from homeassistant.core import HomeAssistant

from .coordinator import FrameCoordinator


def make_art_bridge(
    hass: HomeAssistant, coordinator: FrameCoordinator
) -> Callable[[str, Any], None]:
    """Return a thread-safe callback for SamsungTVArt.start_listening.

    The library invokes this from its own receive thread, so we hop onto the
    event loop before touching coordinator state.
    """

    def _callback(event: str, data: Any) -> None:
        hass.loop.call_soon_threadsafe(coordinator.handle_art_event, event, data)

    return _callback
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_art_listener.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/samsungtv_frame/art_listener.py tests/test_art_listener.py
git commit -m "feat: bridge art-listener thread events onto the event loop"
```

---

### Task 6: Entry setup/unload with runtime_data and platform forwarding

**Files:**
- Modify: `custom_components/samsungtv_frame/__init__.py`
- Test: `tests/test_init.py`

**Interfaces:**
- Consumes: `FrameDevice`, `FrameCoordinator`, `FrameConfigEntry`, `make_art_bridge`, `PLATFORMS`.
- Produces: `async_setup_entry`, `async_unload_entry`. Sets `entry.runtime_data = coordinator`,
  starts the art listener, and forwards `PLATFORMS`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_init.py
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsungtv_frame.const import (
    CONF_HOST, CONF_MAC, CONF_TOKEN, DOMAIN,
)


async def test_setup_and_unload(hass, mock_device):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "1.2.3.4", CONF_MAC: "A0:D0:5B:86:CE:B7", CONF_TOKEN: "tok"},
        unique_id="a0:d0:5b:86:ce:b7",
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.samsungtv_frame.FrameDevice", return_value=mock_device
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data is not None
        assert await hass.config_entries.async_unload(entry.entry_id)
        mock_device.async_stop.assert_awaited()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_init.py -v`
Expected: FAIL (`async_setup_entry` not defined).

- [ ] **Step 3: Write `__init__.py`**

```python
"""The Samsung Frame TV integration."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .art_listener import make_art_bridge
from .const import CONF_HOST, CONF_MAC, CONF_TOKEN, PLATFORMS
from .coordinator import FrameConfigEntry, FrameCoordinator
from .device import FrameDevice


async def async_setup_entry(hass: HomeAssistant, entry: FrameConfigEntry) -> bool:
    """Set up Samsung Frame TV from a config entry."""
    device = FrameDevice(
        hass,
        host=entry.data[CONF_HOST],
        mac=entry.data[CONF_MAC],
        token=entry.data.get(CONF_TOKEN),
    )
    coordinator = FrameCoordinator(hass, entry, device)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    # Start push art listener (best-effort; poll heartbeat is the fallback).
    try:
        await device.async_start_art_listener(make_art_bridge(hass, coordinator))
    except Exception:  # noqa: BLE001 - listener is an enhancement, not required
        pass

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FrameConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.device.async_stop()
    return unloaded
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_init.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/samsungtv_frame/__init__.py tests/test_init.py
git commit -m "feat: wire entry setup/unload with runtime_data and art listener"
```

---

### Task 7: Shared entity base + binary_sensor + ENUM sensor

Two read-only entities first (simplest), sharing a base that supplies `device_info`.

**Files:**
- Create: `custom_components/samsungtv_frame/entity.py`
- Create: `custom_components/samsungtv_frame/binary_sensor.py`
- Create: `custom_components/samsungtv_frame/sensor.py`
- Test: `tests/test_binary_sensor.py`, `tests/test_sensor.py`

**Interfaces:**
- Consumes: `FrameCoordinator`, `FrameConfigEntry`, `TvMode`.
- Produces: `class FrameEntity(CoordinatorEntity[FrameCoordinator])` with `device_info` +
  `_attr_has_entity_name = True`; `FrameArtModeBinarySensor`; `FrameTvModeSensor`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_binary_sensor.py
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsungtv_frame.const import (
    CONF_HOST, CONF_MAC, CONF_TOKEN, DOMAIN,
)


async def _setup(hass, mock_device):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "1.2.3.4", CONF_MAC: "A0:D0:5B:86:CE:B7", CONF_TOKEN: "t"},
        unique_id="a0:d0:5b:86:ce:b7",
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.samsungtv_frame.FrameDevice", return_value=mock_device
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_art_binary_sensor_on(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = True
    await _setup(hass, mock_device)
    state = hass.states.get("binary_sensor.samsung_frame_tv_art_mode")
    assert state is not None
    assert state.state == "on"
```

```python
# tests/test_sensor.py
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsungtv_frame.const import (
    CONF_HOST, CONF_MAC, CONF_TOKEN, DOMAIN,
)


async def test_tv_mode_sensor_watching(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "1.2.3.4", CONF_MAC: "A0:D0:5B:86:CE:B7", CONF_TOKEN: "t"},
        unique_id="a0:d0:5b:86:ce:b7",
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.samsungtv_frame.FrameDevice", return_value=mock_device
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    state = hass.states.get("sensor.samsung_frame_tv_tv_mode")
    assert state.state == "watching"
    assert state.attributes["options"] == ["off", "watching", "art_mode"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_binary_sensor.py tests/test_sensor.py -v`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Write `entity.py`**

```python
"""Shared base entity for Samsung Frame TV."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_MODEL, DOMAIN
from .coordinator import FrameCoordinator


class FrameEntity(CoordinatorEntity[FrameCoordinator]):
    """Base for all Frame entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: FrameCoordinator) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        mac = entry.data["mac"]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, mac)},
            connections={("mac", mac)},
            manufacturer="Samsung",
            model=entry.data.get(CONF_MODEL, "The Frame"),
            name="Samsung Frame TV",
        )
```

- [ ] **Step 4: Write `binary_sensor.py`**

```python
"""Art-mode binary sensor for Samsung Frame TV."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import FrameConfigEntry
from .entity import FrameEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FrameConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([FrameArtModeBinarySensor(entry.runtime_data)])


class FrameArtModeBinarySensor(FrameEntity, BinarySensorEntity):
    """True when the TV is displaying art mode."""

    _attr_translation_key = "art_mode"
    _attr_name = "Art mode"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.data['mac']}_art_mode"

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.art_mode
```

- [ ] **Step 5: Write `sensor.py`**

```python
"""TV-mode ENUM sensor for Samsung Frame TV — automation source of truth."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import FrameConfigEntry
from .entity import FrameEntity
from .models import TvMode

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FrameConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([FrameTvModeSensor(entry.runtime_data)])


class FrameTvModeSensor(FrameEntity, SensorEntity):
    """off / watching / art_mode — the entity automations trigger on."""

    _attr_translation_key = "tv_mode"
    _attr_name = "TV mode"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [TvMode.OFF, TvMode.WATCHING, TvMode.ART_MODE]

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.data['mac']}_tv_mode"

    @property
    def native_value(self) -> str | None:
        mode = self.coordinator.data.tv_mode
        return mode if mode in self._attr_options else None
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_binary_sensor.py tests/test_sensor.py -v`
Expected: PASS. (If entity_ids differ, read `hass.states.async_entity_ids()` and align the
slug — HA derives it from device name + entity name.)

- [ ] **Step 7: Commit**

```bash
git add custom_components/samsungtv_frame/entity.py \
        custom_components/samsungtv_frame/binary_sensor.py \
        custom_components/samsungtv_frame/sensor.py \
        tests/test_binary_sensor.py tests/test_sensor.py
git commit -m "feat: add art-mode binary_sensor and tv_mode ENUM sensor"
```

---

### Task 8: media_player entity — state, turn_on (WoL), turn_off (3s hold)

**Files:**
- Create: `custom_components/samsungtv_frame/media_player.py`
- Test: `tests/test_media_player.py`

**Interfaces:**
- Consumes: `FrameEntity`, `FrameConfigEntry`, `TvMode`.
- Produces: `class FrameMediaPlayer(FrameEntity, MediaPlayerEntity)` mapping `TvMode` → standard
  `MediaPlayerState` and supporting `TURN_ON`/`TURN_OFF`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_media_player.py
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsungtv_frame.const import (
    CONF_HOST, CONF_MAC, CONF_TOKEN, DOMAIN,
)


async def _setup(hass, mock_device):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_HOST: "1.2.3.4", CONF_MAC: "A0:D0:5B:86:CE:B7", CONF_TOKEN: "t"},
        unique_id="a0:d0:5b:86:ce:b7",
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.samsungtv_frame.FrameDevice", return_value=mock_device
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_media_player_reports_playing_when_watching(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    await _setup(hass, mock_device)
    state = hass.states.get("media_player.samsung_frame_tv")
    assert state is not None
    assert state.state == "playing"


async def test_turn_off_calls_device(hass, mock_device):
    mock_device.async_device_info.return_value = {"PowerState": "on"}
    mock_device.async_get_artmode.return_value = False
    await _setup(hass, mock_device)
    await hass.services.async_call(
        "media_player", "turn_off",
        {"entity_id": "media_player.samsung_frame_tv"}, blocking=True,
    )
    mock_device.async_turn_off.assert_awaited_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_media_player.py -v`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Write `media_player.py`**

```python
"""Media player entity for Samsung Frame TV (P1a: state + power)."""
from __future__ import annotations

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import FrameConfigEntry
from .entity import FrameEntity
from .models import TvMode

PARALLEL_UPDATES = 0

_MODE_TO_STATE = {
    TvMode.OFF: MediaPlayerState.OFF,
    TvMode.WATCHING: MediaPlayerState.PLAYING,
    TvMode.ART_MODE: MediaPlayerState.ON,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FrameConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([FrameMediaPlayer(entry.runtime_data)])


class FrameMediaPlayer(FrameEntity, MediaPlayerEntity):
    """Standard media_player surface; art lives in the sensors, never here."""

    _attr_name = None  # main feature of the device
    _attr_device_class = MediaPlayerDeviceClass.TV
    _attr_supported_features = (
        MediaPlayerEntityFeature.TURN_ON | MediaPlayerEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.config_entry.data["mac"]

    @property
    def state(self) -> MediaPlayerState | None:
        return _MODE_TO_STATE.get(self.coordinator.data.tv_mode)

    async def async_turn_on(self) -> None:
        await self.coordinator.device.async_turn_on()

    async def async_turn_off(self) -> None:
        await self.coordinator.device.async_turn_off()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_media_player.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/samsungtv_frame/media_player.py tests/test_media_player.py
git commit -m "feat: add media_player entity with WoL turn-on and 3s-hold turn-off"
```

---

### Task 9: Config flow — manual setup, Frame validation, one-time pairing, dedupe

**Files:**
- Create: `custom_components/samsungtv_frame/config_flow.py`
- Create: `custom_components/samsungtv_frame/strings.json`
- Create: `custom_components/samsungtv_frame/translations/en.json` (copy of strings.json)
- Test: `tests/test_config_flow.py`

**Interfaces:**
- Consumes: constants, `FrameDevice` (for validation/pairing).
- Produces: `class SamsungFrameConfigFlow(ConfigFlow, domain=DOMAIN)` with `async_step_user`;
  helper `async def _pair_and_validate(host) -> dict` returning `{mac, token, model}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_flow.py
from unittest.mock import AsyncMock, patch

from homeassistant.data_entry_flow import FlowResultType

from custom_components.samsungtv_frame.const import DOMAIN


async def test_user_flow_success(hass):
    with patch(
        "custom_components.samsungtv_frame.config_flow.validate_and_pair",
        new=AsyncMock(return_value={"mac": "A0:D0:5B:86:CE:B7",
                                    "token": "tok", "model": "QE65LS03BAUXXH"}),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["type"] == FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "1.2.3.4"}
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["mac"] == "A0:D0:5B:86:CE:B7"
    assert result["data"]["token"] == "tok"


async def test_user_flow_not_a_frame(hass):
    from custom_components.samsungtv_frame.config_flow import NotAFrameError
    with patch(
        "custom_components.samsungtv_frame.config_flow.validate_and_pair",
        new=AsyncMock(side_effect=NotAFrameError),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "1.2.3.4"}
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "not_a_frame"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_config_flow.py -v`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Write `config_flow.py`**

```python
"""Config flow for Samsung Frame TV."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import format_mac
from samsungtvws.async_rest import SamsungTVAsyncRest
from samsungtvws.art import SamsungTVArt

from .const import CLIENT_NAME, CONF_HOST, CONF_MAC, CONF_MODEL, CONF_TOKEN, DOMAIN, PORT_REST, PORT_WS


class NotAFrameError(Exception):
    """The target device is not a Frame TV."""


class CannotConnect(Exception):
    """Could not reach the TV."""


async def validate_and_pair(hass, host: str) -> dict[str, Any]:
    """Confirm it is a Frame, then pair (one-time Allow) and capture the token."""
    session = async_get_clientsession(hass)
    rest = SamsungTVAsyncRest(host, session=session, port=PORT_REST, timeout=8)
    try:
        info = (await rest.rest_device_info()) or {}
    except Exception as err:  # noqa: BLE001
        raise CannotConnect from err
    device = info.get("device", {})
    if device.get("FrameTVSupport") != "true":
        raise NotAFrameError

    def _pair() -> str | None:
        art = SamsungTVArt(host, port=PORT_WS, name=CLIENT_NAME, timeout=30)
        art.open()  # triggers on-TV Allow prompt; returns after acceptance
        token = art.token
        art.close()
        return token

    token = await hass.async_add_executor_job(_pair)
    return {
        CONF_MAC: device.get("wifiMac"),
        CONF_TOKEN: token,
        CONF_MODEL: device.get("modelName"),
    }


class SamsungFrameConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Samsung Frame TV config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                paired = await validate_and_pair(self.hass, user_input[CONF_HOST])
            except NotAFrameError:
                errors["base"] = "not_a_frame"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(format_mac(paired[CONF_MAC]))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=paired[CONF_MODEL] or "Samsung Frame TV",
                    data={CONF_HOST: user_input[CONF_HOST], **paired},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_HOST): str}),
            errors=errors,
        )
```

- [ ] **Step 4: Write `strings.json`**

```json
{
  "config": {
    "step": {
      "user": {
        "title": "Samsung Frame TV",
        "description": "Enter the TV's IP address. Accept the 'Allow' prompt on the TV when it appears.",
        "data": {"host": "IP address"}
      }
    },
    "error": {
      "cannot_connect": "Could not reach the TV. Check the IP and that it is powered on.",
      "not_a_frame": "This Samsung TV does not report Frame support."
    },
    "abort": {
      "already_configured": "This TV is already configured."
    }
  },
  "entity": {
    "binary_sensor": {"art_mode": {"name": "Art mode"}},
    "sensor": {"tv_mode": {"name": "TV mode",
      "state": {"off": "Off", "watching": "Watching", "art_mode": "Art mode"}}}
  }
}
```

- [ ] **Step 5: Copy strings to translations**

Run: `cp custom_components/samsungtv_frame/strings.json custom_components/samsungtv_frame/translations/en.json`
(Create the `translations/` dir first if needed.)

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_config_flow.py -v`
Expected: PASS (2 tests). Note: token capture uses `art.token` — **verified present** on
`SamsungTVWSBaseConnection` (attribute exists, is `None` until the on-TV Allow populates it).

- [ ] **Step 7: Commit**

```bash
git add custom_components/samsungtv_frame/config_flow.py \
        custom_components/samsungtv_frame/strings.json \
        custom_components/samsungtv_frame/translations/en.json \
        tests/test_config_flow.py
git commit -m "feat: add config flow with Frame validation and one-time pairing"
```

---

### Task 10: Full suite green + docs + live acceptance checklist

**Files:**
- Create: `README.md`
- Create: `custom_components/samsungtv_frame/quality_scale.yaml`
- Create: `docs/superpowers/plans/2026-07-01-samsung-frame-p1a-acceptance.md` (manual checklist)

**Interfaces:** none (finalization).

- [ ] **Step 1: Run the complete test suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all tests PASS (models, device, coordinator, art_listener, init, binary_sensor, sensor,
media_player, config_flow).

- [ ] **Step 2: Write `README.md`** (installation, one-time pairing note, the three entities, and
the headline automation example)

````markdown
# Samsung Frame TV (Home Assistant)

Accurate OFF / WATCHING / ART-MODE state for Samsung Frame TVs, plus power control.

## Entities
- `media_player.samsung_frame_tv` — power on (Wake-on-LAN) / off (3 s hold)
- `binary_sensor.samsung_frame_tv_art_mode` — art mode on/off
- `sensor.samsung_frame_tv_tv_mode` — `off` / `watching` / `art_mode` (use this in automations)

## Setup
Settings → Devices & Services → Add Integration → "Samsung Frame TV" → enter the IP.
**Accept the "Allow" prompt on the TV once** (do it while the TV is showing normal content, not
art mode). The token is stored; you won't be asked again unless you reset the TV.

## Example automation
```yaml
triggers:
  - trigger: state
    entity_id: sensor.samsung_frame_tv_tv_mode
    from: art_mode
    to: watching
```
````

- [ ] **Step 3: Write `quality_scale.yaml`** (mark Bronze/Silver rules done/todo honestly)

```yaml
rules:
  config-flow: done
  runtime-data: done
  has-entity-name: done
  entity-unique-id: done
  unique-config-entry: done
  test-before-configure: done
  appropriate-polling: done
  reauthentication-flow: todo   # P1b
  reconfiguration-flow: todo    # P1b
  parallel-updates: done
```

- [ ] **Step 4: Write the live acceptance checklist**

```markdown
# P1a Live Acceptance (run against the real TV at 192.168.33.53)

- [ ] Add integration via UI; accept Allow prompt ONCE (TV in watching mode).
- [ ] Restart HA. Confirm entities reconnect with NO second Allow prompt (token persisted).
- [ ] TV watching Netflix → `sensor...tv_mode` == `watching`, media_player == `playing`.
- [ ] Switch to art mode → within ~1 s `sensor...tv_mode` == `art_mode`, binary_sensor == `on`.
- [ ] Power off (3 s hold) → within ~20 s `sensor...tv_mode` == `off`, media_player == `off`.
- [ ] Call `media_player.turn_on` → TV wakes via WoL.
- [ ] Create the art→watching automation; verify it fires on the transition.
```

- [ ] **Step 5: Commit**

```bash
git add README.md custom_components/samsungtv_frame/quality_scale.yaml \
        docs/superpowers/plans/2026-07-01-samsung-frame-p1a-acceptance.md
git commit -m "docs: add README, quality scale, and live acceptance checklist"
```

---

## Post-P1a (separate plans)
- **P1b — Control surface:** `select_source` (from `app_list`), volume/mute/step, playback keys,
  `send_key` service, `set_art_mode` service, reauth + reconfigure + options flow, diagnostics.
- **P2 — Art management:** `select_image`, `upload_image`, `set_brightness`, `set_matte`,
  `set_slideshow`, `sensor.frame_current_art`.
- **P3 — Release polish:** full translations, brands PR, HACS submission, Gold rules.

## Self-Review Notes
- Spec coverage (P1a subset): tri-state derivation (Task 2), push+poll coordinator (Tasks 4–5),
  one-time pairing (Task 9), three entities (Tasks 7–8), WoL on / 3s-hold off (Tasks 3, 8),
  config flow + dedupe + test-before-configure (Task 9), runtime_data (Task 6). SSDP/DHCP
  discovery matchers declared (Task 1) with discovery *steps* deferred to P1b (documented).
- One flagged verification point carried into execution: `SamsungTVArt.token` attribute name for
  capture (Task 9 Step 6) — confirm against installed lib during that task; fallback documented.
