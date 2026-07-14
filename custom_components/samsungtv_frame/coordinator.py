"""Data update coordinator for a Samsung Frame TV."""
from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .art_session import ArtSessionState
from .const import (
    ART_FAIL_UNKNOWN_COUNT,
    ART_RECONCILE_SECONDS,
    CONF_TOKEN,
    DEFAULT_APP_MAP,
    DEFAULT_HEARTBEAT_SECONDS,
    DOMAIN,
    LOGGER,
    OFF_DEBOUNCE_COUNT,
    OPT_HEARTBEAT,
    POLL_DEADLINE,
    PORT_REST,
    UPNP_FAIL_WARN_COUNT,
    WAKE_PROBE_ATTEMPTS,
    WAKE_PROBE_DELAY,
    WAKE_PROBE_TIMEOUT,
)
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
            update_interval=timedelta(
                seconds=entry.options.get(OPT_HEARTBEAT, DEFAULT_HEARTBEAT_SECONDS)
            ),
            config_entry=entry,
            always_update=False,
        )
        self.device = device
        self._unreachable_count = 0
        self._art_mode: bool | None = None
        self._current_art: str | None = None
        self._art_brightness: int | None = None
        self._art_color_temp: int | None = None
        self._art_generation = -1
        self._next_art_reconcile = 0.0
        self._clock = time.monotonic
        self._art_mode_revision = 0
        self._current_art_revision = 0
        self._art_live_push_revision = 0
        self._latest_rest_power_state: str | None = None
        self._was_reachable = True
        self._wake_task: asyncio.Task | None = None
        self._art_ready_refresh_task: asyncio.Task | None = None
        self._standby_refresh_task: asyncio.Task | None = None
        # Consecutive polls (reachable, power on) with a failed art query;
        # bounds the last-stable hold so a permanently broken art channel
        # surfaces as 'unknown' instead of freezing state forever.
        self._art_fail_streak = 0
        self._art_fail_warned = False
        self._upnp_fail_streak = 0
        self._upnp_fail_warned = False
        # Learned model trait: art mode coexisting with PowerState "on"
        # (2022-24 Frames) means "standby" can only be a shutdown, so it may
        # override the art gate in derive_tv_mode. Never learned on 2025
        # models, which report "standby" during normal art mode (#185).
        self._art_implies_power_on = False
        # Curated built-in apps keyed by display name for media_player sources.
        # Copy each entry so runtime consumers cannot mutate the constants.
        self.app_map: dict[str, dict[str, Any]] = {
            name: dict(app) for name, app in DEFAULT_APP_MAP.items()
        }

    def _last_stable(self) -> TvMode:
        if self.data is not None and self.data.tv_mode is not TvMode.UNKNOWN:
            return self.data.tv_mode
        return TvMode.UNKNOWN

    async def _async_update_data(self) -> FrameData:
        # One wedged device call must never kill the update loop: a poll that
        # blows the deadline fails cleanly and the next interval retries.
        try:
            async with asyncio.timeout(POLL_DEADLINE):
                return await self._async_poll()
        except TimeoutError as err:
            raise UpdateFailed(
                f"TV poll exceeded {POLL_DEADLINE}s deadline"
            ) from err

    async def _async_poll(self) -> FrameData:
        info = await self.device.async_device_info()
        reachable = info is not None
        power_state = info.get("PowerState") if info else None
        self._latest_rest_power_state = power_state

        was_reachable = self._was_reachable
        self._was_reachable = reachable
        reachable_edge = reachable and not was_reachable
        if not reachable or reachable_edge:
            self.device.remote_confirmed = False
        session_power_state = (
            None
            if power_state == "standby" and self._art_implies_power_on
            else power_state
        )
        self.device.observe_art_power(
            reachable, session_power_state, reachable_edge
        )

        if reachable:
            self._unreachable_count = 0
            self._async_capture_token()
        else:
            self._unreachable_count += 1

        generation = self.device.art_generation
        reconcile_due = (
            reachable
            and self.device.art_ready
            and power_state in {"on", "standby"}
            and not (
                power_state == "standby" and self._art_implies_power_on
            )
            and (
                generation != self._art_generation
                or self._clock() >= self._next_art_reconcile
            )
        )
        mode_sample_live = False
        if reconcile_due:
            mode_sample_live = await self._async_reconcile_art(generation)

        if reachable_edge or not reachable or power_state != "on":
            self._reset_art_failure()
        elif reconcile_due:
            if mode_sample_live:
                self._reset_art_failure()
            else:
                self._record_art_failure()
        elif not self.device.art_ready:
            self._record_art_failure()

        effective_art_mode = (
            None
            if self._art_fail_streak >= ART_FAIL_UNKNOWN_COUNT
            else self._art_mode
        )

        # OFF debounce: only declare OFF after N consecutive unreachable polls.
        if not reachable and self._unreachable_count < OFF_DEBOUNCE_COUNT:
            mode = self._last_stable()
        else:
            mode = derive_tv_mode(
                reachable,
                effective_art_mode,
                power_state,
                standby_wins=self._art_implies_power_on,
            )
            if mode is TvMode.UNKNOWN and self._art_fail_streak < ART_FAIL_UNKNOWN_COUNT:
                # Hold last-stable through transient art failures only: a
                # persistently dead art channel must surface as unknown, not
                # freeze the last state forever.
                mode = self._last_stable()

        if mode is TvMode.OFF:
            self.device.remote_confirmed = False

        running_app: str | None = None
        if mode is TvMode.WATCHING and self.app_map:
            running_app = await self._async_detect_running_app()

        volume_level: float | None = None
        is_muted: bool | None = None
        if reachable and power_state == "on":
            volume_level, is_muted = await self.device.async_get_volume()
            if volume_level is None:
                self._upnp_fail_streak += 1
                if self._upnp_fail_streak >= UPNP_FAIL_WARN_COUNT and not self._upnp_fail_warned:
                    self._upnp_fail_warned = True
                    LOGGER.warning(
                        "Volume unavailable via UPnP for %s consecutive polls; "
                        "check that DLNA/DMR is enabled on the TV",
                        self._upnp_fail_streak,
                    )
            else:
                self._upnp_fail_streak = 0
                self._upnp_fail_warned = False

        LOGGER.debug(
            "Poll: reachable=%s power=%s art=%s -> %s",
            reachable, power_state, self._art_mode, mode,
        )

        # When the TV is off we know art mode cannot be active, even if the
        # last cached value says otherwise.  Keep self._art_mode unchanged so
        # the cached state is still valid once the TV comes back.
        is_off = mode is TvMode.OFF
        return FrameData(
            reachable=reachable,
            power_state=power_state,
            art_mode=False if is_off else effective_art_mode,
            tv_mode=mode,
            current_art=None if is_off else self._current_art,
            art_brightness=None if is_off else self._art_brightness,
            art_color_temperature=None if is_off else self._art_color_temp,
            running_app=running_app,
            volume_level=volume_level,
            is_muted=is_muted,
        )

    async def _async_reconcile_art(self, generation: int) -> bool:
        """Refresh cached Art fields over one already-READY generation."""
        self._art_generation = generation
        self._next_art_reconcile = self._clock() + ART_RECONCILE_SECONDS
        mode_revision = self._art_mode_revision
        current_art_revision = self._current_art_revision
        live_push_revision = self._art_live_push_revision

        art_mode = await self.device.async_get_artmode()
        current_art: str | None = None
        art_brightness: int | None = None
        art_color_temp: int | None = None
        current_art_valid = False
        art_brightness_valid = False
        art_color_temp_valid = False

        if self._art_session_is_ready(generation):
            current_art = await self.device.async_get_current_art()
            current_art_valid = self._art_session_is_ready(generation)
        if current_art_valid:
            art_brightness = await self.device.async_get_art_brightness()
            art_brightness_valid = self._art_session_is_ready(generation)
        if art_brightness_valid:
            art_color_temp = await self.device.async_get_color_temperature()
            art_color_temp_valid = self._art_session_is_ready(generation)

        if (
            art_mode is not None
            and self._art_mode_revision == mode_revision
        ):
            self._art_mode = art_mode
            if art_mode and self._latest_rest_power_state == "on":
                self._art_implies_power_on = True
        if (
            current_art_valid
            and self._current_art_revision == current_art_revision
        ):
            self._current_art = current_art
        if art_brightness_valid:
            self._art_brightness = art_brightness
        if art_color_temp_valid:
            self._art_color_temp = art_color_temp

        return (
            art_mode is not None
            or self._art_live_push_revision != live_push_revision
        )

    def _art_session_is_ready(self, generation: int) -> bool:
        """Return whether reconciliation still owns one READY generation."""
        return (
            self.device.art_ready
            and self.device.art_generation == generation
        )

    def _reset_art_failure(self) -> None:
        self._art_fail_streak = 0
        self._art_fail_warned = False

    def _record_art_failure(self) -> None:
        self._art_fail_streak += 1
        if (
            self._art_fail_streak >= ART_FAIL_UNKNOWN_COUNT
            and not self._art_fail_warned
        ):
            self._art_fail_warned = True
            LOGGER.warning(
                "Art websocket has failed %s consecutive polls; "
                "tv_mode will report unknown until it recovers "
                "(check the TV's device connection permissions)",
                self._art_fail_streak,
            )

    async def _async_detect_running_app(self) -> str | None:
        """Name of the foreground app, or None (live TV / HDMI / not found).

        One REST status call per curated app, bounded concurrency; the
        foreground app is the one reporting visible=true.
        """
        assert self.app_map is not None
        sem = asyncio.Semaphore(8)

        async def _check(name: str, app: dict[str, Any]) -> str | None:
            async with sem:
                status = await self.device.async_app_status(app["appId"])
            return name if status and status.get("visible") else None

        results = await asyncio.gather(
            *(_check(name, app) for name, app in self.app_map.items())
        )
        return next((name for name in results if name), None)

    @callback
    def handle_remote_token(self, token: str) -> None:
        """Adopt and persist a changed canonical remote token."""
        if not token:
            return
        if token != self.config_entry.data.get(CONF_TOKEN):
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, CONF_TOKEN: token},
            )
            LOGGER.info("Captured a newly issued remote credential")
        # Runtime adoption acknowledges successful persistence. If the entry
        # already held this token, this still repairs stale runtime clients.
        self.device.update_token(token)

    @callback
    def handle_remote_reauth(self) -> None:
        """Start Home Assistant reauthorization for this entry."""
        self.config_entry.async_start_reauth(self.hass)

    @callback
    def _async_capture_token(self) -> None:
        """Persist a remote token observed by the heartbeat safety net.

        Foreground operations capture first; this path only covers a remote
        token that became visible outside the immediate command lifecycle.
        """
        token = self.device.newest_token
        if token is not None:
            self.handle_remote_token(token)

    @callback
    def async_notify_turn_on(self) -> None:
        """Kick a fast wake probe after turn_on (WoL) was sent.

        The heartbeat alone reacts slowly to wake-up: while the TV boots,
        each poll burns the full REST timeout before the next one is even
        scheduled, so consecutive polls end up ~18 s apart. Probing the REST
        port directly is cheap, so we can afford a tight loop and refresh
        the moment the TV answers.
        """
        if self._wake_task is not None and not self._wake_task.done():
            return
        self._wake_task = self.config_entry.async_create_background_task(
            self.hass, self._wake_probe(), f"{DOMAIN}-wake-probe"
        )

    async def _wake_probe(self) -> None:
        for _ in range(WAKE_PROBE_ATTEMPTS):
            if self.data is not None and self.data.tv_mode is not TvMode.OFF:
                return
            if await self._async_probe_port():
                # Debounced (immediate on first call), so the repeat calls
                # while the art socket isn't ready yet stay cheap.
                await self.async_request_refresh()
            await asyncio.sleep(WAKE_PROBE_DELAY)
        LOGGER.warning(
            "TV did not respond within %.0f s of the Wake-on-LAN packet; "
            "check that WoL is enabled on the TV and the stored MAC matches "
            "its active network interface",
            WAKE_PROBE_ATTEMPTS * WAKE_PROBE_DELAY,
        )

    async def _async_probe_port(self) -> bool:
        try:
            async with asyncio.timeout(WAKE_PROBE_TIMEOUT):
                _, writer = await asyncio.open_connection(self.device.host, PORT_REST)
        except Exception:  # noqa: BLE001 - any failure just means "not up yet"
            return False
        writer.close()
        return True

    @callback
    def handle_art_session_state(self, state: ArtSessionState) -> None:
        """Refresh once when a new Art receiver generation becomes ready."""
        if state is not ArtSessionState.READY:
            return
        if (
            self._art_ready_refresh_task is not None
            and not self._art_ready_refresh_task.done()
        ):
            return
        self._art_ready_refresh_task = (
            self.config_entry.async_create_background_task(
                self.hass,
                self.async_request_refresh(),
                f"{DOMAIN}-art-ready-refresh",
            )
        )

    @callback
    def handle_art_event(self, event: str, data: Any) -> None:
        """Handle an unsolicited Art event on the Home Assistant loop."""
        LOGGER.debug("Art event received")
        sub = data.get("event") if isinstance(data, dict) else None
        if sub in ("art_mode_changed", "artmode_status"):
            value = data.get("value") or data.get("status")
            self._art_mode = value == "on"
            self._art_mode_revision += 1
            if self._art_mode and self._latest_rest_power_state == "on":
                self._art_implies_power_on = True
        elif sub in ("image_selected", "slideshow_image_changed", "image_changed"):
            content_id = data.get("content_id")
            if not content_id:
                return
            self._current_art = content_id
            self._current_art_revision += 1
        elif sub == "go_to_standby":
            self._art_live_push_revision += 1
            self._reset_art_failure()
            # Never a state by itself (the destination is ambiguous), but a
            # strong hint the TV is shutting down: refresh now instead of
            # waiting out the heartbeat, so OFF lands in seconds.
            if (
                self._standby_refresh_task is None
                or self._standby_refresh_task.done()
            ):
                self._standby_refresh_task = (
                    self.config_entry.async_create_background_task(
                        self.hass,
                        self.async_request_refresh(),
                        f"{DOMAIN}-standby-refresh",
                    )
                )
            return
        else:
            return
        self._art_live_push_revision += 1
        self._reset_art_failure()
        current = self.data
        # Use the last polled PowerState and the learned trait: during the
        # shutdown window the dying art socket can still push "on" events,
        # and standby must keep winning exactly as it does on the poll path.
        power_state = self._latest_rest_power_state
        if power_state is None:
            power_state = current.power_state if current else "on"
        mode = derive_tv_mode(
            True, self._art_mode, power_state,
            standby_wins=self._art_implies_power_on,
        )
        if mode is TvMode.UNKNOWN:
            mode = self._last_stable()
        if mode is TvMode.OFF:
            self.device.remote_confirmed = False
        self.async_set_updated_data(
            FrameData(
                reachable=True,
                power_state=power_state,
                art_mode=self._art_mode,
                tv_mode=mode,
                current_art=self._current_art,
                art_brightness=current.art_brightness if current else None,
                art_color_temperature=(
                    current.art_color_temperature if current else None
                ),
                running_app=current.running_app if current else None,
                volume_level=current.volume_level if current else None,
                is_muted=current.is_muted if current else None,
            )
        )
