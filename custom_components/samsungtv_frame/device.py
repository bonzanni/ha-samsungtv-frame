"""Async facade over the samsungtvws library for a Frame TV."""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from async_upnp_client.aiohttp import AiohttpSessionRequester
from async_upnp_client.client import UpnpDevice, UpnpService
from async_upnp_client.client_factory import UpnpFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from samsungtvws.async_rest import SamsungTVAsyncRest
from samsungtvws.command import SamsungTVCommand
from samsungtvws.exceptions import ConnectionFailure, ResponseError
from samsungtvws.remote import ChannelEmitCommand, SendRemoteKey
from wakeonlan import send_magic_packet

from .const import (
    ART_CLOSE_DEADLINE,
    DOMAIN,
    LOGGER,
    PORT_REST,
)
from .art_session import (
    ArtSession,
    ArtSessionState,
    ArtSessionTrigger,
    StateCallback,
)
from .frame_art import ArtEventCallback, FrameArt, TaskFactory
from .frame_remote import FrameRemote, RemotePairingRequired

_RENDERING_CONTROL = "urn:schemas-upnp-org:service:RenderingControl:1"
_DMR_URL = "http://{host}:9197/dmr"


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
        self._rest: SamsungTVAsyncRest | None = None
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
        # UPnP DMR device (RenderingControl) — created lazily, dropped on
        # failure so a TV power cycle just triggers a fresh description fetch.
        self._upnp_device: UpnpDevice | None = None
        self._remote_token_callback: Callable[[str], None] | None = None
        self._remote_reauth_callback: Callable[[], None] | None = None
        # True after any successful remote-channel operation this run. The
        # background app-list fetch is gated on it: a remote connect can make
        # the TV pop an authorization prompt (e.g. after a power cycle wiped
        # the grant), and only user-initiated actions should ever do that.
        self.remote_confirmed = False

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

    def _ensure_rest(self) -> SamsungTVAsyncRest:
        if self._rest is None:
            session = async_get_clientsession(self._hass)
            self._rest = SamsungTVAsyncRest(
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
        except ResponseError:
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

    async def async_get_art_brightness(self) -> int | None:
        value = await self._async_art_read(self._art.get_brightness)
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

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

    async def async_get_color_temperature(self) -> int | None:
        value = await self._async_art_read(
            self._art.get_color_temperature
        )
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

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

    async def async_turn_on(self) -> None:
        await self._hass.async_add_executor_job(
            lambda: send_magic_packet(self._mac, ip_address="255.255.255.255")
        )

    async def async_turn_off(self) -> None:
        # Single press only toggles art mode; a 3 s hold truly powers a Frame off.
        await self._async_remote_commands(SendRemoteKey.hold("KEY_POWER", 3))

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
        """Installed apps, or None (not supported on all TVs / TV not ready)."""
        remote = self._remote
        if remote is None:
            return None
        try:
            apps = await remote.app_list()
        except RemotePairingRequired:
            self.remote_confirmed = False
            return None
        except Exception:  # noqa: BLE001
            LOGGER.debug("Remote app-list request failed")
            return None
        self.remote_confirmed = True
        self._capture_remote_token()
        return apps

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
        """Send on the persistent remote; reset once if the connection is stale.

        Like the art client, the remote keeps a cached connection that is never
        invalidated when the TV power-cycles; the first send after a cycle
        fails, so close and retry once before giving up.
        """
        remote = self._remote
        if remote is None:
            self._request_remote_reauth()
            raise RemotePairingRequired("Remote authorization required")
        try:
            await remote.send_commands(commands)
        except RemotePairingRequired:
            self._request_remote_reauth()
            raise
        except Exception:  # noqa: BLE001
            LOGGER.debug(
                "Remote send failed; retrying once on a fresh connection"
            )
            with contextlib.suppress(Exception, TimeoutError):
                async with asyncio.timeout(ART_CLOSE_DEADLINE):
                    await remote.close()
            try:
                await remote.send_commands(commands)
            except RemotePairingRequired:
                self._request_remote_reauth()
                raise
        self.remote_confirmed = True
        self._capture_remote_token()

    def set_art_event_callback(self, callback: ArtEventCallback) -> None:
        """Set the loop-native callback receiving unsolicited Art events."""
        self._art.set_event_callback(callback)

    async def _async_close_remote(self) -> None:
        """Close the remote without allowing shutdown to wedge indefinitely."""
        remote = self._remote
        if remote is None:
            return
        async with asyncio.timeout(ART_CLOSE_DEADLINE):
            await remote.close()

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
        completion = asyncio.gather(
            self._art_session.async_stop(),
            self._async_close_remote(),
            return_exceptions=True,
        )
        while True:
            try:
                await asyncio.shield(completion)
                return
            except asyncio.CancelledError:
                if completion.cancelled():
                    raise
