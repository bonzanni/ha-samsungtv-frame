"""Single-owner lifecycle supervisor for the Frame Art transport."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
import contextlib
from enum import StrEnum
import random
import time
from typing import Any, cast

from samsungtvws.exceptions import ConnectionFailure

from .const import (
    ART_DORMANT_SECONDS,
    ART_HOST_RETRY_DELAYS,
    ART_RETRY_DELAYS,
    ART_RETRY_JITTER,
    DOMAIN,
    LOGGER,
)
from .frame_art import ArtHostUnavailable, FrameArt, TaskFactory


class ArtSessionState(StrEnum):
    """Lifecycle states for the supervised Art transport."""

    STOPPED = "stopped"
    CONNECTING = "connecting"
    READY = "ready"
    BACKOFF = "backoff"
    DORMANT = "dormant"


class ArtSessionTrigger(StrEnum):
    """Reasons an Art connection may be requested."""

    BACKGROUND = "background"
    POWER_EDGE = "power_edge"
    USER = "user"


type StateCallback = Callable[[ArtSessionState], None]
type Clock = Callable[[], float]
type Jitter = Callable[[float], float]


def _default_jitter(delay: float) -> float:
    spread = delay * ART_RETRY_JITTER
    return random.uniform(delay - spread, delay + spread)


class ArtSession:
    """Own connection attempts and recovery policy for one Art transport."""

    def __init__(
        self,
        art: FrameArt,
        *,
        task_factory: TaskFactory,
        state_callback: StateCallback | None = None,
        clock: Clock = time.monotonic,
        jitter: Jitter = _default_jitter,
    ) -> None:
        self._art = art
        self._task_factory = task_factory
        self._state_callback = state_callback
        self._clock = clock
        self._jitter = jitter
        self._connect_task: asyncio.Task[bool] | None = None
        self._started = False
        self._terminal = False
        self._failure_count = 0
        self._host_failure_count = 0
        self._next_retry_at = 0.0
        self._last_reachable: bool | None = None
        self._generation = 0
        self._state = ArtSessionState.STOPPED

    @property
    def state(self) -> ArtSessionState:
        """Return the current lifecycle state."""
        return self._state

    @property
    def generation(self) -> int:
        """Return the number of successful transport generations."""
        return self._generation

    @property
    def ready(self) -> bool:
        """Return whether the current receiver generation is alive."""
        return (
            self._state is ArtSessionState.READY and self._art.is_alive()
        )

    def set_state_callback(self, callback: StateCallback | None) -> None:
        """Replace the state callback without replaying current state."""
        self._state_callback = callback

    async def async_start(self) -> None:
        """Enable recovery without opening the Art transport."""
        if self._started or self._terminal:
            return
        self._started = True
        self._reset_failures()
        self._next_retry_at = 0.0
        self._set_state(ArtSessionState.BACKOFF)

    def observe_power(
        self,
        reachable: bool,
        power_state: str,
        reachable_edge: bool,
    ) -> None:
        """Record power state and schedule due connection work."""
        if not self._started:
            return
        self._last_reachable = reachable
        if reachable_edge:
            self._reset_failures()
            self._next_retry_at = 0.0
        if not reachable or power_state != "on":
            return
        if (
            self._state is ArtSessionState.READY
            and not self._art.is_alive()
        ):
            self._record_failure(
                ConnectionFailure("Art receiver stopped")
            )
        if self._background_attempt_due():
            self._schedule_connect(
                ArtSessionTrigger.POWER_EDGE
                if reachable_edge
                else ArtSessionTrigger.BACKGROUND
            )

    async def async_ensure_ready(
        self, trigger: ArtSessionTrigger
    ) -> bool:
        """Return readiness after joining or starting one allowed attempt."""
        if not self._started or self._terminal:
            return False
        if self.ready:
            return True

        task = self._active_connect_task()
        if task is None:
            bypass = trigger in {
                ArtSessionTrigger.USER,
                ArtSessionTrigger.POWER_EDGE,
            }
            if not bypass and not self._background_attempt_due():
                return False
            task = self._schedule_connect(trigger)
        if task is None:
            return False
        return await asyncio.shield(task)

    async def async_connection_failed(self, error: Exception) -> None:
        """Close a failed transport and enter its next recovery state."""
        if not self._started or self._terminal:
            return
        await self._close_transport()
        if self._started and not self._terminal:
            self._record_failure(error)

    async def async_stop(self) -> None:
        """Permanently stop this session and close its transport."""
        if self._terminal:
            return
        self._terminal = True
        self._started = False
        task = self._active_connect_task()
        try:
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise
        finally:
            self._connect_task = None
            self._set_state(ArtSessionState.STOPPED)
            self._art.stop()
            await self._close_transport()

    def _set_state(self, state: ArtSessionState) -> None:
        if state is self._state:
            return
        self._state = state
        callback = self._state_callback
        if callback is not None:
            try:
                callback(state)
            except Exception as err:  # noqa: BLE001
                LOGGER.warning("Art session state callback failed: %s", err)

    def _reset_failures(self) -> None:
        self._failure_count = 0
        self._host_failure_count = 0

    def _record_failure(self, error: Exception) -> None:
        self._failure_count += 1
        now = self._clock()
        if isinstance(error, ArtHostUnavailable):
            self._host_failure_count += 1
            if self._host_failure_count >= 3:
                self._next_retry_at = now + ART_DORMANT_SECONDS
                self._set_state(ArtSessionState.DORMANT)
                return
            delay = ART_HOST_RETRY_DELAYS[
                min(
                    self._host_failure_count - 1,
                    len(ART_HOST_RETRY_DELAYS) - 1,
                )
            ]
        else:
            self._host_failure_count = 0
            delay = ART_RETRY_DELAYS[
                min(self._failure_count - 1, len(ART_RETRY_DELAYS) - 1)
            ]
        self._next_retry_at = now + self._jitter(delay)
        self._set_state(ArtSessionState.BACKOFF)

    def _background_attempt_due(self) -> bool:
        return (
            self._started
            and not self._terminal
            and self._state
            in {ArtSessionState.BACKOFF, ArtSessionState.DORMANT}
            and self._active_connect_task() is None
            and self._clock() >= self._next_retry_at
        )

    def _active_connect_task(self) -> asyncio.Task[bool] | None:
        task = self._connect_task
        if task is not None and task.done():
            self._connect_task = None
            return None
        return task

    def _schedule_connect(
        self, trigger: ArtSessionTrigger
    ) -> asyncio.Task[bool] | None:
        del trigger
        task = self._active_connect_task()
        if task is not None:
            return task
        if not self._started or self._terminal or self.ready:
            return None

        coroutine = self._connect_once()
        try:
            task = cast(
                asyncio.Task[bool],
                self._task_factory(
                    cast(Coroutine[Any, Any, None], coroutine),
                    f"{DOMAIN}-art-session-connect",
                ),
            )
        except BaseException:
            coroutine.close()
            raise
        self._connect_task = task
        task.add_done_callback(self._connect_finished)
        return task

    def _connect_finished(self, task: asyncio.Task[bool]) -> None:
        if self._connect_task is task:
            self._connect_task = None

    async def _connect_once(self) -> bool:
        self._set_state(ArtSessionState.CONNECTING)
        try:
            await self._art.start_listening()
            current = asyncio.current_task()
            if (
                not self._started
                or self._terminal
                or (current is not None and current.cancelling())
            ):
                raise asyncio.CancelledError
            if not self._art.is_alive():
                raise ConnectionFailure("Art receiver did not start")
        except asyncio.CancelledError:
            raise
        except ArtHostUnavailable as err:
            if self._started and not self._terminal:
                self._record_failure(err)
            await self._close_transport()
            return False
        except Exception as err:  # noqa: BLE001
            if self._started and not self._terminal:
                self._record_failure(err)
            await self._close_transport()
            return False

        self._generation += 1
        self._reset_failures()
        self._next_retry_at = 0.0
        self._set_state(ArtSessionState.READY)
        return True

    async def _close_transport(self) -> None:
        with contextlib.suppress(Exception):
            await self._art.close()
