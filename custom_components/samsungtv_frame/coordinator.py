"""Data update coordinator for a Samsung Frame TV."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Awaitable, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    APP_FETCH_MAX_ATTEMPTS,
    APP_FETCH_POLL_SPACING,
    ART_FAIL_UNKNOWN_COUNT,
    DEFAULT_APP_MAP,
    CONF_TOKEN,
    DEFAULT_HEARTBEAT_SECONDS,
    DOMAIN,
    LOGGER,
    OFF_DEBOUNCE_COUNT,
    OPT_HEARTBEAT,
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
        self._was_reachable = True
        self._wake_task: asyncio.Task | None = None
        self._listener_task: asyncio.Task | None = None
        # Consecutive polls (reachable, power on) with a failed art query;
        # bounds the last-stable hold so a permanently broken art channel
        # surfaces as 'unknown' instead of freezing state forever.
        self._art_fail_streak = 0
        self._art_fail_warned = False
        self._upnp_fail_streak = 0
        self._upnp_fail_warned = False
        self._app_fetch_warned = False
        # Learned model trait: art mode coexisting with PowerState "on"
        # (2022-24 Frames) means "standby" can only be a shutdown, so it may
        # override the art gate in derive_tv_mode. Never learned on 2025
        # models, which report "standby" during normal art mode (#185).
        self._art_implies_power_on = False
        # Installed apps keyed by display name (media_player source list).
        # Seeded with the built-in catalog so the source dropdown exists from
        # the first poll; replaced by the TV's real list when (if) the TV
        # answers the request — some firmwares never do.
        self.app_map: dict[str, dict[str, Any]] = {
            name: dict(app) for name, app in DEFAULT_APP_MAP.items()
        }
        self._app_map_is_fallback = True
        self._app_fetch_attempts = 0
        self._app_fetch_countdown = 0
        # Set by __init__.py once the initial art listener + bridge callback
        # exist, so a power-cycle recovery can restart the SAME listener.
        self.restart_listener: Callable[[], Awaitable[None]] | None = None

    def _last_stable(self) -> TvMode:
        if self.data is not None and self.data.tv_mode is not TvMode.UNKNOWN:
            return self.data.tv_mode
        return TvMode.UNKNOWN

    async def _async_update_data(self) -> FrameData:
        info = await self.device.async_device_info()
        reachable = info is not None
        power_state = info.get("PowerState") if info else None

        was_reachable = self._was_reachable
        self._was_reachable = reachable
        if reachable and not was_reachable:
            self._async_kick_listener_restart()
            # New power-on: the app list may have changed (and the previous
            # attempts may have hit a booting TV) — the real list gets a new
            # shot; the catalog keeps serving sources meanwhile.
            self._app_fetch_attempts = 0
            self._app_fetch_countdown = 0
            self._app_fetch_warned = False
        elif (
            reachable
            and power_state == "on"
            and self.restart_listener is not None
            and not self.device.listener_alive
        ):
            # The recv thread died without an unreachable poll in between
            # (e.g. a brief WiFi blip the heartbeat never saw). Skipped in
            # standby: the TV is shutting down and a reconnect would hang.
            self._async_kick_listener_restart()

        if reachable:
            self._unreachable_count = 0
            # A TV reporting standby is (on this model) shutting down and its
            # art socket tends to hang until timeout — a retry would double
            # the poll latency for nothing, and a single failed attempt
            # already resolves to OFF via derive_tv_mode.
            attempts = 1 if power_state == "standby" else 2
            self._art_mode = await self.device.async_get_artmode(attempts=attempts)
            self._async_capture_token()
            if self._art_mode is None and power_state == "on":
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
            elif self._art_mode is not None:
                self._art_fail_streak = 0
                self._art_fail_warned = False
            if self._art_mode is True and power_state == "on":
                self._art_implies_power_on = True
                # Art extras ride the same (healthy) art socket. Cached values
                # persist while WATCHING: the selection stays valid until an
                # image_selected push or the next art-mode poll changes it.
                self._current_art = await self.device.async_get_current_art()
                self._art_brightness = await self.device.async_get_art_brightness()
                self._art_color_temp = (
                    await self.device.async_get_color_temperature()
                )
        else:
            self._unreachable_count += 1

        # OFF debounce: only declare OFF after N consecutive unreachable polls.
        if not reachable and self._unreachable_count < OFF_DEBOUNCE_COUNT:
            mode = self._last_stable()
        else:
            mode = derive_tv_mode(
                reachable,
                self._art_mode,
                power_state,
                standby_wins=self._art_implies_power_on,
            )
            if mode is TvMode.UNKNOWN and self._art_fail_streak < ART_FAIL_UNKNOWN_COUNT:
                # Hold last-stable through transient art failures only: a
                # persistently dead art channel must surface as unknown, not
                # freeze the last state forever.
                mode = self._last_stable()

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

        # Real app-list fetch: retry on every APP_FETCH_POLL_SPACING-th poll
        # so the attempts span minutes — a cold-booting TV ignores the request
        # for the first ~30 s, which would burn back-to-back attempts.
        if (
            mode in (TvMode.WATCHING, TvMode.ART_MODE)
            and self._app_map_is_fallback
            and self._app_fetch_attempts < APP_FETCH_MAX_ATTEMPTS
        ):
            self._app_fetch_countdown -= 1
            if self._app_fetch_countdown <= 0:
                self._app_fetch_countdown = APP_FETCH_POLL_SPACING
                self._app_fetch_attempts += 1
                self.config_entry.async_create_background_task(
                    self.hass, self._async_fetch_app_list(), f"{DOMAIN}-app-list"
                )

        # When the TV is off we know art mode cannot be active, even if the
        # last cached value says otherwise.  Keep self._art_mode unchanged so
        # the cached state is still valid once the TV comes back.
        is_off = mode is TvMode.OFF
        return FrameData(
            reachable=reachable,
            power_state=power_state,
            art_mode=False if is_off else self._art_mode,
            tv_mode=mode,
            current_art=None if is_off else self._current_art,
            art_brightness=None if is_off else self._art_brightness,
            art_color_temperature=None if is_off else self._art_color_temp,
            running_app=running_app,
            volume_level=volume_level,
            is_muted=is_muted,
        )

    async def _async_detect_running_app(self) -> str | None:
        """Name of the foreground app, or None (live TV / HDMI / not found).

        One REST status call per installed app, bounded concurrency; the
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

    async def _async_fetch_app_list(self) -> None:
        """Populate the media_player source list from the TV's installed apps."""
        apps = await self.device.async_app_list()
        if not apps:
            if (
                self._app_fetch_attempts >= APP_FETCH_MAX_ATTEMPTS
                and not self._app_fetch_warned
            ):
                self._app_fetch_warned = True
                # Some firmwares (e.g. 2022 LS03B) accept the request but
                # never answer it; the pre-seeded catalog keeps serving.
                LOGGER.info(
                    "The TV did not answer the installed-apps request after "
                    "%s attempts; keeping the built-in catalog of %s "
                    "well-known apps",
                    self._app_fetch_attempts,
                    len(self.app_map),
                )
            return
        self.app_map = {
            app["name"]: app for app in apps if app.get("name") and app.get("appId")
        }
        self._app_map_is_fallback = False
        LOGGER.debug("Fetched %d installed apps", len(self.app_map))
        self.async_update_listeners()

    @callback
    def _async_capture_token(self) -> None:
        """Persist a token the TV issued after pairing, if one appeared.

        Pairing granted access by client name without issuing a token; if the
        TV hands one out on any later connection, store it in the config entry
        so reconnects (and TV-side grant loss) don't depend on the name grant.
        """
        token = self.device.newest_token
        if token is None or token == self.config_entry.data.get(CONF_TOKEN):
            return
        self.device.update_token(token)
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_TOKEN: token},
        )
        LOGGER.info("Captured a newly issued TV token into the config entry")

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
    def _async_kick_listener_restart(self) -> None:
        """Schedule one listener restart; no-op while one is in flight.

        Entry-scoped so unload cancels it, deduped so a flapping TV cannot
        pile up concurrent restarts.
        """
        if self.restart_listener is None:
            return
        if self._listener_task is not None and not self._listener_task.done():
            return
        self._listener_task = self.config_entry.async_create_background_task(
            self.hass, self._restart_listener_safe(), f"{DOMAIN}-listener-restart"
        )

    async def _restart_listener_safe(self) -> None:
        """Restart the art push listener, swallowing failures.

        A failure here must never break the polling loop; the liveness check
        in the next poll schedules another attempt.
        """
        try:
            await self.restart_listener()
        except Exception as err:  # noqa: BLE001
            LOGGER.warning(
                "Could not restart the art event listener (%s); "
                "retrying on a later poll, push updates degraded to polling",
                err,
            )

    @callback
    def handle_art_event(self, event: str, data: Any) -> None:
        """Loop-safe handler for pushed art events (see Task 5 bridge)."""
        LOGGER.debug("Art event: %s", data)
        sub = data.get("event") if isinstance(data, dict) else None
        if sub in ("art_mode_changed", "artmode_status"):
            value = data.get("value") or data.get("status")
            self._art_mode = value == "on"
        elif sub in ("image_selected", "slideshow_image_changed", "image_changed"):
            content_id = data.get("content_id")
            if not content_id:
                return
            self._current_art = content_id
        elif sub == "go_to_standby":
            # Never a state by itself (the destination is ambiguous), but a
            # strong hint the TV is shutting down: refresh now instead of
            # waiting out the heartbeat, so OFF lands in seconds.
            self.hass.async_create_task(self.async_request_refresh())
            return
        else:
            return
        current = self.data
        # Use the last polled PowerState and the learned trait: during the
        # shutdown window the dying art socket can still push "on" events,
        # and standby must keep winning exactly as it does on the poll path.
        power_state = current.power_state if current else "on"
        mode = derive_tv_mode(
            True, self._art_mode, power_state,
            standby_wins=self._art_implies_power_on,
        )
        if mode is TvMode.UNKNOWN:
            mode = self._last_stable()
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
