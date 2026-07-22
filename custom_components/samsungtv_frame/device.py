"""Async facade over the samsungtvws library for a Frame TV."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from async_upnp_client.aiohttp import AiohttpSessionRequester
from async_upnp_client.client import UpnpDevice, UpnpService
from async_upnp_client.client_factory import UpnpFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from samsungtvws.command import SamsungTVCommand
from samsungtvws.exceptions import (
    ConnectionFailure,
    ResponseError,
    UnauthorizedError,
)
from samsungtvws.remote import ChannelEmitCommand, SendRemoteKey
from wakeonlan import send_magic_packet

from .art_session import (
    ArtSession,
    ArtSessionState,
    ArtSessionTrigger,
    StateCallback,
)
from .art_settings import normalize_art_setting, parse_art_settings, parse_slideshow
from .const import (
    DOMAIN,
    LOGGER,
    PORT_REST,
    REMOTE_CANCEL_DEADLINE,
    REMOTE_CLOSE_DEADLINE,
    REMOTE_DRAIN_DEADLINE,
)
from .frame_art import (
    ArtEventCallback,
    ArtProbeTimeout,
    FrameArt,
    InvalidArtSettingError,
    TaskFactory,
)
from .frame_remote import FrameRemote, RemotePairingRequired
from .models import ArtSettingKey, ArtSettingsSnapshot, SlideshowState
from .rest import PrivacySafeSamsungTVAsyncRest

_RENDERING_CONTROL = "urn:schemas-upnp-org:service:RenderingControl:1"
_DMR_URL = "http://{host}:9197/dmr"
_ART_READ_FAILED = object()


class _ArtSettingsDialect(StrEnum):
    """Generation-scoped Art settings command dialect."""

    UNKNOWN = "unknown"
    AGGREGATE = "aggregate"
    LEGACY = "legacy"


class _SlideshowDialect(StrEnum):
    """Generation-scoped slideshow command dialect."""

    UNKNOWN = "unknown"
    AUTO_ROTATION = "auto_rotation"
    LEGACY = "legacy"
    UNSUPPORTED = "unsupported"


class FrameDevice:
    """Clean async surface the coordinator talks to."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        mac: str,
        token: str | None,
        *,
        ssl_context: Any,
        task_factory: TaskFactory,
    ) -> None:
        self._hass = hass
        self._host = host
        self._mac = mac
        self._token = token
        self._ssl_context = ssl_context
        self._task_factory = task_factory
        # _rest is created lazily on first async_device_info call: aiohttp's connector
        # requires a running event loop, so we cannot create it here in __init__ (which
        # may be called from a sync context, e.g. in tests).
        self._rest: PrivacySafeSamsungTVAsyncRest | None = None
        self._remote: FrameRemote | None = (
            self._create_remote(token) if token else None
        )
        self._art = FrameArt(
            host,
            token=token,
            ssl_context=ssl_context,
            task_factory=task_factory,
            event_callback=None,
        )
        self._art_session = ArtSession(
            self._art,
            task_factory=task_factory,
        )
        # Once stopped (entry unload), no listener may be (re)started — an
        # in-flight restart task finishing after unload would otherwise
        # resurrect a connection nothing will ever close.
        self._stopped = False
        self._stop_task: asyncio.Task[None] | None = None
        self._remote_operation_lock = asyncio.Lock()
        self._active_remote_operation: asyncio.Task[Any] | None = None
        self._remote_quiescing = False
        # UPnP DMR device (RenderingControl) — created lazily, dropped on
        # failure so a TV power cycle just triggers a fresh description fetch.
        self._upnp_device: UpnpDevice | None = None
        self._remote_token_callback: Callable[[str], None] | None = None
        self._remote_reauth_callback: Callable[[], None] | None = None
        # True after any successful foreground remote-channel operation this
        # run; power and unload boundaries invalidate the observation.
        self.remote_confirmed = False
        self._optional_dialect_generation: int | None = None
        self._art_settings_dialect = _ArtSettingsDialect.UNKNOWN
        self._slideshow_dialect = _SlideshowDialect.UNKNOWN

    @property
    def host(self) -> str:
        return self._host

    @property
    def art_ready(self) -> bool:
        """Return whether the supervised Art receiver is ready."""
        return self._art_session.ready

    @property
    def art_generation(self) -> int:
        """Return the current successful Art transport generation."""
        return self._art_session.generation

    @property
    def art_session_state(self) -> ArtSessionState:
        """Return the supervised Art lifecycle state."""
        return self._art_session.state

    def observe_art_power(
        self,
        reachable: bool,
        power_state: str | None,
        reachable_edge: bool,
    ) -> None:
        """Synchronously pass a power observation to the Art session."""
        self._art_session.observe_power(
            reachable, power_state, reachable_edge
        )

    def set_art_session_state_callback(
        self, callback: StateCallback | None
    ) -> None:
        """Replace the Art session state callback."""
        self._art_session.set_state_callback(callback)

    async def async_start_art_session(self) -> None:
        """Arm supervised Art recovery without opening the transport."""
        await self._art_session.async_start()

    @property
    def newest_token(self) -> str | None:
        """A remote-issued token that differs from the one we hold, if any.

        The remote channel owns the canonical credential. Art-channel values
        are deliberately ignored by this heartbeat compatibility path.
        """
        remote = self._remote
        if remote is None:
            return None
        token = remote.token
        return token if token and token != self._token else None

    def _create_remote(self, token: str) -> FrameRemote:
        """Create a credentialed remote; callers must supply a token."""
        return FrameRemote(
            self._host,
            token=token,
            ssl_context=self._ssl_context,
            timeout=8,
        )

    def update_token(self, token: str) -> None:
        """Adopt a newly issued token for all future (re)connections."""
        if not token:
            return
        remote = self._remote
        if remote is None:
            remote = self._create_remote(token)
        self._token = token
        self._art.token = token
        remote.token = token
        self._remote = remote

    def set_remote_token_callback(
        self, callback: Callable[[str], None] | None
    ) -> None:
        """Replace the synchronous remote-token persistence callback."""
        self._remote_token_callback = callback

    def set_remote_reauth_callback(
        self, callback: Callable[[], None] | None
    ) -> None:
        """Replace the foreground remote reauthorization callback."""
        self._remote_reauth_callback = callback

    def _ensure_rest(self) -> PrivacySafeSamsungTVAsyncRest:
        if self._rest is None:
            session = async_get_clientsession(self._hass)
            self._rest = PrivacySafeSamsungTVAsyncRest(
                self._host, session=session, port=PORT_REST, timeout=8
            )
        return self._rest

    async def async_device_info(self) -> dict[str, Any] | None:
        try:
            info = await self._ensure_rest().rest_device_info()
        except Exception:  # noqa: BLE001 - library raises broad connection types
            LOGGER.debug("REST device info request failed")
            return None
        return info.get("device") if info else None

    async def _async_rendering_control(self) -> UpnpService:
        if self._upnp_device is None:
            session = async_get_clientsession(self._hass)
            factory = UpnpFactory(AiohttpSessionRequester(session), non_strict=True)
            self._upnp_device = await factory.async_create_device(
                _DMR_URL.format(host=self._host)
            )
        return self._upnp_device.service(_RENDERING_CONTROL)

    async def async_get_volume(self) -> tuple[float | None, bool | None]:
        """(volume_level 0-1, muted) via UPnP, or (None, None)."""
        try:
            rc = await self._async_rendering_control()
            vol = await rc.action("GetVolume").async_call(
                InstanceID=0, Channel="Master"
            )
            mute = await rc.action("GetMute").async_call(
                InstanceID=0, Channel="Master"
            )
        except Exception:  # noqa: BLE001
            LOGGER.debug("UPnP volume query failed")
            self._upnp_device = None
            return None, None
        return vol["CurrentVolume"] / 100, bool(mute["CurrentMute"])

    async def async_set_volume(self, level: float) -> None:
        try:
            rc = await self._async_rendering_control()
            await rc.action("SetVolume").async_call(
                InstanceID=0, Channel="Master", DesiredVolume=round(level * 100)
            )
        except Exception:
            self._upnp_device = None
            raise

    async def async_set_mute(self, mute: bool) -> None:
        try:
            rc = await self._async_rendering_control()
            await rc.action("SetMute").async_call(
                InstanceID=0, Channel="Master", DesiredMute=mute
            )
        except Exception:
            self._upnp_device = None
            raise

    async def async_app_status(self, app_id: str) -> dict[str, Any] | None:
        """Status payload for one app ({visible, running, ...}), or None."""
        try:
            return await self._ensure_rest().rest_app_status(app_id)
        except Exception:  # noqa: BLE001
            LOGGER.debug("REST app status request failed")
            return None

    async def _async_art_read(
        self, operation: Callable[[], Awaitable[Any]]
    ) -> Any:
        """Run one background read only on an already-ready session."""
        if self._stopped or not self._art_session.ready:
            return None
        try:
            return await operation()
        except ResponseError:
            return None
        except Exception as err:  # noqa: BLE001
            await self._art_session.async_connection_failed(err)
            return None

    async def _async_art_read_response(
        self, operation: Callable[[], Awaitable[Any]]
    ) -> Any:
        """Run an optional read while preserving correlated response errors."""
        if self._stopped or not self._art_session.ready:
            return _ART_READ_FAILED
        try:
            return await operation()
        except (ResponseError, ArtProbeTimeout):
            raise
        except Exception as err:  # noqa: BLE001
            await self._art_session.async_connection_failed(err)
            return _ART_READ_FAILED

    def _reset_optional_dialects_for_generation(self, generation: int) -> None:
        """Reset optional command choices at each ready generation boundary."""
        if self._optional_dialect_generation == generation:
            return
        self._optional_dialect_generation = generation
        self._art_settings_dialect = _ArtSettingsDialect.UNKNOWN
        self._slideshow_dialect = _SlideshowDialect.UNKNOWN

    def _optional_generation_is_current(self, generation: int) -> bool:
        """Return whether an optional response is fresh for this generation."""
        return (
            not self._stopped
            and self.art_ready
            and self.art_generation == generation
        )

    async def _async_get_legacy_art_settings(
        self,
        generation: int,
        *,
        liveness_proven: bool = False,
    ) -> ArtSettingsSnapshot | None:
        """Read supported legacy settings without confusing absence and loss."""
        supported: set[ArtSettingKey] = set()
        normalized: dict[ArtSettingKey, int | str | bool | None] = {}
        last_timeout: ArtProbeTimeout | None = None
        getters = (
            (ArtSettingKey.BRIGHTNESS, self._art.get_legacy_brightness),
            (
                ArtSettingKey.COLOR_TEMPERATURE,
                self._art.get_legacy_color_temperature,
            ),
        )
        for key, getter in getters:
            try:
                value = await self._async_art_read_response(getter)
            except ResponseError:
                liveness_proven = True
                continue
            except ArtProbeTimeout as err:
                last_timeout = err
                continue

            if value is _ART_READ_FAILED:
                return None
            if not self._optional_generation_is_current(generation):
                return None
            liveness_proven = True
            supported.add(key)
            normalized[key] = normalize_art_setting(key, value)

        if not self._optional_generation_is_current(generation):
            return None
        if not liveness_proven:
            assert last_timeout is not None
            await self._art_session.async_connection_failed(last_timeout)
            return None

        self._art_settings_dialect = _ArtSettingsDialect.LEGACY
        return ArtSettingsSnapshot(
            supported=frozenset(supported),
            brightness=normalized.get(ArtSettingKey.BRIGHTNESS),
            color_temperature=normalized.get(
                ArtSettingKey.COLOR_TEMPERATURE
            ),
        )

    async def _async_art_mutation(
        self, operation: Callable[[], Awaitable[Any]]
    ) -> Any:
        """Run one user mutation after one session-owned readiness probe."""
        if self._stopped:
            raise ConnectionFailure("Art device is stopped")
        if not await self._art_session.async_ensure_ready(
            ArtSessionTrigger.USER
        ):
            raise ConnectionFailure("Art session is unavailable")
        try:
            return await operation()
        except (ResponseError, InvalidArtSettingError):
            raise
        except Exception as err:  # noqa: BLE001
            await self._art_session.async_connection_failed(err)
            raise

    async def async_get_artmode(self) -> bool | None:
        """Return Art Mode state without opening or retrying the session."""
        value = await self._async_art_read(self._art.get_artmode)
        if value is None:
            return None
        return value == "on"

    async def async_set_artmode(self, on: bool) -> None:
        await self._async_art_mutation(lambda: self._art.set_artmode(on))

    async def async_get_current_art(self) -> str | None:
        """Content id of the artwork currently selected, or None."""
        current = await self._async_art_read(self._art.get_current)
        if isinstance(current, dict):
            return current.get("content_id")
        return None

    async def async_get_art_settings(self) -> ArtSettingsSnapshot | None:
        """Return normalized settings with same-pass liveness evidence."""
        generation = self.art_generation
        self._reset_optional_dialects_for_generation(generation)
        if self._art_settings_dialect is _ArtSettingsDialect.LEGACY:
            return await self._async_get_legacy_art_settings(generation)

        liveness_proven = False
        try:
            payload = await self._async_art_read_response(
                self._art.get_art_settings_payload
            )
        except ResponseError:
            liveness_proven = True
        except ArtProbeTimeout:
            pass
        else:
            if (
                payload is _ART_READ_FAILED
                or not self._optional_generation_is_current(generation)
            ):
                return None
            snapshot = (
                parse_art_settings(payload)
                if isinstance(payload, dict)
                else None
            )
            if (
                snapshot is not None
                and self._optional_generation_is_current(generation)
            ):
                self._art_settings_dialect = _ArtSettingsDialect.AGGREGATE
            return snapshot

        if not self._optional_generation_is_current(generation):
            return None
        return await self._async_get_legacy_art_settings(
            generation,
            liveness_proven=liveness_proven,
        )

    async def async_get_slideshow_state(self) -> SlideshowState | None:
        """Return slideshow state using same-pass liveness evidence."""
        generation = self.art_generation
        self._reset_optional_dialects_for_generation(generation)
        dialect = self._slideshow_dialect
        if dialect is _SlideshowDialect.UNSUPPORTED:
            return None

        if dialect is _SlideshowDialect.LEGACY:
            try:
                payload = await self._async_art_read_response(
                    self._art.get_legacy_slideshow_status
                )
            except ResponseError:
                if self._optional_generation_is_current(generation):
                    self._slideshow_dialect = _SlideshowDialect.UNSUPPORTED
                return None
            except ArtProbeTimeout as err:
                if self._optional_generation_is_current(generation):
                    await self._art_session.async_connection_failed(err)
                return None
            if (
                payload is _ART_READ_FAILED
                or not self._optional_generation_is_current(generation)
            ):
                return None
            return (
                parse_slideshow(payload)
                if isinstance(payload, dict)
                else None
            )

        liveness_proven = False
        try:
            payload = await self._async_art_read_response(
                self._art.get_auto_rotation_status
            )
        except ResponseError:
            liveness_proven = True
        except ArtProbeTimeout:
            pass
        else:
            if (
                payload is _ART_READ_FAILED
                or not self._optional_generation_is_current(generation)
            ):
                return None
            self._slideshow_dialect = _SlideshowDialect.AUTO_ROTATION
            return (
                parse_slideshow(payload)
                if isinstance(payload, dict)
                else None
            )

        if not self._optional_generation_is_current(generation):
            return None
        try:
            payload = await self._async_art_read_response(
                self._art.get_legacy_slideshow_status
            )
        except ResponseError:
            if self._optional_generation_is_current(generation):
                self._slideshow_dialect = _SlideshowDialect.UNSUPPORTED
            return None
        except ArtProbeTimeout as err:
            if not self._optional_generation_is_current(generation):
                return None
            if liveness_proven:
                self._slideshow_dialect = _SlideshowDialect.UNSUPPORTED
            else:
                await self._art_session.async_connection_failed(err)
            return None

        if (
            payload is _ART_READ_FAILED
            or not self._optional_generation_is_current(generation)
        ):
            return None
        self._slideshow_dialect = _SlideshowDialect.LEGACY
        return parse_slideshow(payload) if isinstance(payload, dict) else None

    async def async_set_art_brightness(self, value: int) -> None:
        await self._async_art_mutation(
            lambda: self._art.set_brightness(value)
        )

    async def async_select_art(self, content_id: str, show: bool) -> None:
        await self._async_art_mutation(
            lambda: self._art.select_image(content_id, None, show)
        )

    async def async_upload_art(
        self, data: bytes, file_type: str, matte: str
    ) -> str:
        """Upload image bytes; returns the TV-assigned content id."""
        return await self._async_art_mutation(
            lambda: self._art.upload(data, file_type, matte)
        )

    async def async_delete_art(self, content_id: str) -> None:
        await self._async_art_mutation(
            lambda: self._art.delete(content_id)
        )

    async def async_get_art_thumbnail(self, content_id: str) -> bytes | None:
        """JPEG thumbnail bytes for an artwork, or None.

        Store artworks (SAM-*) are DRM-refused by the TV and yield None.
        """
        if self._stopped or not self._art_session.ready:
            return None
        try:
            return await self._art.get_thumbnail(content_id)
        except Exception:  # noqa: BLE001
            LOGGER.debug("Thumbnail fetch failed")
            return None

    async def async_change_matte(self, content_id: str, matte_id: str) -> None:
        await self._async_art_mutation(
            lambda: self._art.change_matte(content_id, matte_id)
        )

    async def async_set_photo_filter(self, content_id: str, filter_id: str) -> None:
        await self._async_art_mutation(
            lambda: self._art.set_photo_filter(content_id, filter_id)
        )

    async def async_set_favourite(self, content_id: str, favourite: bool) -> None:
        await self._async_art_mutation(
            lambda: self._art.set_favourite(content_id, favourite)
        )

    async def async_set_color_temperature(self, value: int) -> None:
        await self._async_art_mutation(
            lambda: self._art.set_color_temperature(value)
        )

    async def async_set_slideshow(
        self, duration: int, shuffle: bool, category_id: str
    ) -> None:
        """Configure the art slideshow."""
        await self._async_art_mutation(
            lambda: self._art.set_slideshow(duration, shuffle, category_id)
        )

    async def async_set_motion_timer(self, value: str) -> None:
        """Set the Art Mode motion timer after one user readiness probe."""
        await self._async_art_mutation(
            lambda: self._art.set_motion_timer(value)
        )

    async def async_set_motion_sensitivity(self, value: str) -> None:
        """Set motion sensitivity after one user readiness probe."""
        await self._async_art_mutation(
            lambda: self._art.set_motion_sensitivity(value)
        )

    async def async_set_brightness_sensor(self, enabled: bool) -> None:
        """Set automatic brightness after one user readiness probe."""
        await self._async_art_mutation(
            lambda: self._art.set_brightness_sensor_setting(enabled)
        )

    async def async_turn_on(self) -> None:
        await self._hass.async_add_executor_job(
            lambda: send_magic_packet(self._mac, ip_address="255.255.255.255")
        )

    async def async_turn_off(self) -> None:
        # Single press only toggles art mode; a 3 s hold truly powers a Frame off.
        try:
            await self._async_remote_commands(
                SendRemoteKey.hold("KEY_POWER", 3)
            )
        finally:
            # A successful power-off makes this transport authorization
            # observation stale; a failed send leaves the TV state unknown.
            self.remote_confirmed = False

    async def async_send_key(self, key: str) -> None:
        await self._async_remote_commands([SendRemoteKey.click(key)])

    async def async_hold_key(self, key: str, seconds: float) -> None:
        await self._async_remote_commands(SendRemoteKey.hold(key, seconds))

    async def async_launch_app(
        self, app_id: str, app_type: str, meta_tag: str = ""
    ) -> None:
        await self._async_remote_commands(
            [ChannelEmitCommand.launch_app(app_id, app_type, meta_tag)]
        )

    async def async_app_list(self) -> list[dict[str, Any]] | None:
        """Compatibility seam; runtime installed-app discovery is disabled."""
        return None

    @property
    def _remote_unavailable(self) -> bool:
        return self._stopped or self._remote_quiescing

    def _ensure_remote_available(self) -> None:
        if self._remote_unavailable:
            raise ConnectionFailure("Remote control is unavailable")

    async def async_quiesce_remote(self) -> None:
        """Close remote admission and join operations before reversible unload."""
        self._remote_quiescing = True
        self.remote_confirmed = False
        await self._async_drain_remote_operation()

    def resume_remote(self) -> None:
        """Reopen remote admission after a failed platform unload."""
        if not self._stopped:
            self._remote_quiescing = False

    async def _async_drain_remote_operation(self) -> None:
        """Boundedly join, then cancel, the active serialized remote work."""
        operation = self._active_remote_operation
        if operation is None or operation is asyncio.current_task():
            return
        _, pending = await asyncio.wait(
            {operation}, timeout=REMOTE_DRAIN_DEADLINE
        )
        if not pending:
            return
        operation.cancel()
        _, pending = await asyncio.wait(
            {operation}, timeout=REMOTE_CANCEL_DEADLINE
        )
        if pending:
            raise ConnectionFailure("Remote operation did not stop")

    def _capture_remote_token(self) -> None:
        """Synchronously persist a changed canonical remote token."""
        remote = self._remote
        if remote is None:
            return
        token = remote.token
        if (
            token
            and token != self._token
            and self._remote_token_callback is not None
        ):
            self._remote_token_callback(token)

    def _request_remote_reauth(self) -> None:
        """Signal that a foreground operation needs TV authorization."""
        self.remote_confirmed = False
        if self._remote_reauth_callback is not None:
            self._remote_reauth_callback()

    async def _async_remote_commands(self, commands: list[SamsungTVCommand]) -> None:
        """Open, persist authorization, then send; retry one stale transport.

        Opening is explicit so a token issued during authorization is persisted
        before a power command can make the TV unreachable. Persistence sits
        outside the transport exception handlers: a callback failure must fail
        the operation without sending or being retried as a stale socket.
        """
        self._ensure_remote_available()
        async with self._remote_operation_lock:
            operation = asyncio.current_task()
            self._active_remote_operation = operation
            try:
                self._ensure_remote_available()
                remote = self._remote
                if remote is None:
                    self._request_remote_reauth()
                    raise RemotePairingRequired(
                        "Remote authorization required"
                    )

                for attempt in range(2):
                    try:
                        await remote.open()
                    except RemotePairingRequired:
                        self._request_remote_reauth()
                        raise
                    except UnauthorizedError:
                        self._request_remote_reauth()
                        raise UnauthorizedError(
                            "Remote authorization rejected"
                        ) from None
                    except Exception:  # noqa: BLE001
                        self.remote_confirmed = False
                        if attempt == 1:
                            raise ConnectionFailure(
                                "Remote control connection failed"
                            ) from None
                        LOGGER.debug(
                            "Remote open failed; retrying once on a fresh connection"
                        )
                        await self._async_reset_remote(remote)
                        continue

                    self._capture_remote_token()
                    self._ensure_remote_available()

                    try:
                        await remote.send_commands(commands)
                    except RemotePairingRequired:
                        self._request_remote_reauth()
                        raise
                    except UnauthorizedError:
                        self._request_remote_reauth()
                        raise UnauthorizedError(
                            "Remote authorization rejected"
                        ) from None
                    except Exception:  # noqa: BLE001
                        self.remote_confirmed = False
                        if attempt == 1:
                            raise ConnectionFailure(
                                "Remote control connection failed"
                            ) from None
                        LOGGER.debug(
                            "Remote send failed; retrying once on a fresh connection"
                        )
                        await self._async_reset_remote(remote)
                        continue

                    self._ensure_remote_available()
                    self.remote_confirmed = True
                    return
            finally:
                if self._active_remote_operation is operation:
                    self._active_remote_operation = None

    async def _async_reset_remote(self, remote: FrameRemote) -> None:
        """Close one stale socket; do not retry unless ownership is resolved."""
        try:
            await remote.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ConnectionFailure("Remote control reset failed") from None

    def set_art_event_callback(self, callback: ArtEventCallback) -> None:
        """Set the loop-native callback receiving unsolicited Art events."""
        self._art.set_event_callback(callback)

    async def _async_stop_remote(self) -> None:
        """Join active remote work, then permanently stop its transport."""
        drain_error: ConnectionFailure | None = None
        try:
            await self._async_drain_remote_operation()
        except ConnectionFailure as err:
            drain_error = err
        remote = self._remote
        if remote is not None:
            try:
                async with asyncio.timeout(REMOTE_CLOSE_DEADLINE):
                    await remote.async_stop()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        if drain_error is not None:
            raise drain_error

    async def async_stop(self) -> None:
        """Join the one task-factory-owned terminal shutdown."""
        while True:
            task = self._stop_task
            if task is not None and task.done() and (
                task.cancelled() or task.exception() is not None
            ):
                self._stop_task = None
                task = None
            if task is None:
                coroutine = self._async_stop_once()
                try:
                    task = self._task_factory(
                        coroutine, f"{DOMAIN}-device-stop"
                    )
                except BaseException:
                    coroutine.close()
                    raise
                self._stop_task = task
                self._stopped = True
                self._remote_quiescing = True
                self.remote_confirmed = False
            try:
                await asyncio.shield(task)
                return
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                if task.cancelled():
                    if self._stop_task is task:
                        self._stop_task = None
                    continue
                raise

    async def _async_stop_once(self) -> None:
        """Stop the Art session and remote exactly once."""
        self._stopped = True
        self._remote_quiescing = True
        self.remote_confirmed = False
        completion = asyncio.gather(
            self._art_session.async_stop(),
            self._async_stop_remote(),
            return_exceptions=True,
        )
        while True:
            try:
                await asyncio.shield(completion)
                return
            except asyncio.CancelledError:
                if completion.cancelled():
                    raise
