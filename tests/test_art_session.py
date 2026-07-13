"""Tests for the single-owner Art session supervisor."""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Coroutine
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from samsungtvws.exceptions import ConnectionFailure

from custom_components.samsungtv_frame import art_session as art_session_module
from custom_components.samsungtv_frame.art_session import (
    ArtSession,
    ArtSessionState,
    ArtSessionTrigger,
)
from custom_components.samsungtv_frame.frame_art import ArtHostUnavailable


class FakeClock:
    """Mutable monotonic clock for deterministic retry tests."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class RecordingTaskFactory:
    """HA-compatible task factory that records session-owned tasks."""

    def __init__(self) -> None:
        self.calls: list[tuple[Coroutine[Any, Any, Any], str]] = []

    def __call__(
        self, coroutine: Coroutine[Any, Any, Any], name: str
    ) -> asyncio.Task[Any]:
        self.calls.append((coroutine, name))
        return asyncio.create_task(coroutine, name=name)


class FakeArt:
    """Controllable Art transport with counted lifecycle methods."""

    def __init__(self, outcomes: list[Exception | None] | None = None) -> None:
        self.outcomes = deque(outcomes or [])
        self.alive = False
        self._start_gate: asyncio.Event | None = None
        self._ignore_start_cancellation = False
        self._close_gate: asyncio.Event | None = None
        self.start_listening = AsyncMock(side_effect=self._start_listening)
        self.close = AsyncMock(side_effect=self._close)
        self.stop = MagicMock(side_effect=self._stop)
        self.is_alive = MagicMock(side_effect=lambda: self.alive)

    def block_next_start(self, *, ignore_cancellation: bool = False) -> None:
        self._start_gate = asyncio.Event()
        self._ignore_start_cancellation = ignore_cancellation

    def release_start(self) -> None:
        assert self._start_gate is not None
        self._start_gate.set()

    def block_next_close(self) -> None:
        self._close_gate = asyncio.Event()

    def release_close(self) -> None:
        assert self._close_gate is not None
        self._close_gate.set()

    async def _start_listening(self) -> None:
        gate = self._start_gate
        ignore_cancellation = self._ignore_start_cancellation
        self._ignore_start_cancellation = False
        if gate is not None:
            try:
                while True:
                    try:
                        await gate.wait()
                        break
                    except asyncio.CancelledError:
                        if not ignore_cancellation:
                            raise
            finally:
                self._start_gate = None
        outcome = self.outcomes.popleft() if self.outcomes else None
        if outcome is not None:
            raise outcome
        self.alive = True

    async def _close(self) -> None:
        gate = self._close_gate
        if gate is not None:
            try:
                await gate.wait()
            finally:
                self._close_gate = None
        self.alive = False

    def _stop(self) -> None:
        self.alive = False


def make_session(
    art: FakeArt,
    clock: FakeClock,
    *,
    task_factory: RecordingTaskFactory | None = None,
    state_callback=None,
) -> tuple[ArtSession, RecordingTaskFactory]:
    factory = task_factory or RecordingTaskFactory()
    return (
        ArtSession(
            art,
            task_factory=factory,
            state_callback=state_callback,
            clock=clock,
            jitter=lambda delay: delay,
        ),
        factory,
    )


async def wait_for_start_calls(art: FakeArt, expected: int) -> None:
    for _ in range(20):
        if art.start_listening.await_count == expected:
            return
        await asyncio.sleep(0)
    raise AssertionError(
        f"expected {expected} start calls, got {art.start_listening.await_count}"
    )


async def wait_for_close_calls(art: FakeArt, expected: int) -> None:
    for _ in range(20):
        if art.close.await_count == expected:
            return
        await asyncio.sleep(0)
    raise AssertionError(
        f"expected {expected} close calls, got {art.close.await_count}"
    )


async def wait_for_stop_calls(art: FakeArt, expected: int) -> None:
    for _ in range(20):
        if art.stop.call_count == expected:
            return
        await asyncio.sleep(0)
    raise AssertionError(
        f"expected {expected} stop calls, got {art.stop.call_count}"
    )


def shorten_lifecycle_deadlines(monkeypatch) -> None:
    monkeypatch.setattr(
        art_session_module, "ART_CONNECT_DEADLINE", 0.01, raising=False
    )
    monkeypatch.setattr(
        art_session_module, "ART_CLOSE_DEADLINE", 0.01, raising=False
    )


async def test_concurrent_callers_share_one_connect_task():
    art = FakeArt()
    art.block_next_start()
    clock = FakeClock()
    session, factory = make_session(art, clock)
    await session.async_start()

    first = asyncio.create_task(
        session.async_ensure_ready(ArtSessionTrigger.USER)
    )
    second = asyncio.create_task(
        session.async_ensure_ready(ArtSessionTrigger.USER)
    )
    await wait_for_start_calls(art, 1)

    art.release_start()
    assert await asyncio.gather(first, second) == [True, True]
    assert art.start_listening.await_count == 1
    assert len(factory.calls) == 1


async def test_generic_failures_follow_30_60_120_300_backoff():
    art = FakeArt([ConnectionFailure("nope") for _ in range(5)])
    clock = FakeClock()
    session, _ = make_session(art, clock)
    await session.async_start()

    for expected_delay in (30, 60, 120, 300, 300):
        assert not await session.async_ensure_ready(
            ArtSessionTrigger.BACKGROUND
        )
        assert session._next_retry_at - clock.now == expected_delay
        clock.now = session._next_retry_at


async def test_observations_inside_backoff_do_not_connect():
    art = FakeArt([ConnectionFailure("nope")])
    clock = FakeClock()
    session, _ = make_session(art, clock)
    await session.async_start()

    assert not await session.async_ensure_ready(ArtSessionTrigger.BACKGROUND)
    for _ in range(30):
        session.observe_power(
            reachable=True, power_state="on", reachable_edge=False
        )
        await asyncio.sleep(0)
    assert art.start_listening.await_count == 1


async def test_three_hostless_failures_enter_900_second_dormant():
    art = FakeArt(
        [ArtHostUnavailable("no host") for _ in range(3)]
    )
    clock = FakeClock()
    session, _ = make_session(art, clock)
    await session.async_start()

    assert not await session.async_ensure_ready(
        ArtSessionTrigger.BACKGROUND
    )
    assert session.state is ArtSessionState.BACKOFF
    clock.now = session._next_retry_at
    assert not await session.async_ensure_ready(
        ArtSessionTrigger.BACKGROUND
    )
    assert session.state is ArtSessionState.BACKOFF
    clock.now = session._next_retry_at
    assert not await session.async_ensure_ready(
        ArtSessionTrigger.BACKGROUND
    )
    assert session.state is ArtSessionState.DORMANT
    assert session._next_retry_at == clock.now + 900


async def test_dormant_allows_exactly_one_half_open_probe():
    art = FakeArt(
        [ArtHostUnavailable("no host") for _ in range(3)]
    )
    clock = FakeClock()
    session, _ = make_session(art, clock)
    await session.async_start()

    for _ in range(3):
        assert not await session.async_ensure_ready(
            ArtSessionTrigger.BACKGROUND
        )
        clock.now = session._next_retry_at
    dormant_deadline = session._next_retry_at
    art.block_next_start()

    clock.now = dormant_deadline - 1
    for _ in range(5):
        session.observe_power(
            reachable=True, power_state="on", reachable_edge=False
        )
        await asyncio.sleep(0)
    assert art.start_listening.await_count == 3

    clock.now = dormant_deadline
    for _ in range(5):
        session.observe_power(
            reachable=True, power_state="on", reachable_edge=False
        )
        await asyncio.sleep(0)
    assert art.start_listening.await_count == 4

    art.release_start()
    await asyncio.sleep(0)
    await session.async_stop()


async def test_reachable_edge_resets_failures_and_allows_one_probe():
    art = FakeArt([ConnectionFailure("nope"), None])
    clock = FakeClock()
    session, _ = make_session(art, clock)
    await session.async_start()
    assert not await session.async_ensure_ready(ArtSessionTrigger.BACKGROUND)
    assert session._failure_count == 1

    session.observe_power(
        reachable=True, power_state="on", reachable_edge=True
    )
    for _ in range(10):
        session.observe_power(
            reachable=True, power_state="on", reachable_edge=False
        )
        await asyncio.sleep(0)

    assert art.start_listening.await_count == 2
    assert session.state is ArtSessionState.READY
    assert session._failure_count == 0
    assert session._host_failure_count == 0


async def test_user_trigger_bypasses_suppression_once():
    art = FakeArt(
        [ArtHostUnavailable("no host") for _ in range(3)]
    )
    clock = FakeClock()
    session, _ = make_session(art, clock)
    await session.async_start()
    for _ in range(3):
        assert not await session.async_ensure_ready(
            ArtSessionTrigger.BACKGROUND
        )
        clock.now = session._next_retry_at
    assert session.state is ArtSessionState.DORMANT
    art.block_next_start()

    first = asyncio.create_task(
        session.async_ensure_ready(ArtSessionTrigger.USER)
    )
    second = asyncio.create_task(
        session.async_ensure_ready(ArtSessionTrigger.USER)
    )
    await wait_for_start_calls(art, 4)
    art.release_start()

    assert await asyncio.gather(first, second) == [True, True]
    assert art.start_listening.await_count == 4


async def test_ready_resets_failures_and_increments_generation():
    art = FakeArt([ConnectionFailure("nope"), None])
    clock = FakeClock()
    session, _ = make_session(art, clock)
    await session.async_start()

    assert not await session.async_ensure_ready(ArtSessionTrigger.BACKGROUND)
    clock.now = session._next_retry_at
    assert await session.async_ensure_ready(ArtSessionTrigger.BACKGROUND)

    assert session.state is ArtSessionState.READY
    assert session.ready
    assert session.generation == 1
    assert session._failure_count == 0
    assert session._host_failure_count == 0
    assert session._next_retry_at == 0


async def test_dead_ready_receiver_enters_backoff_without_immediate_open():
    art = FakeArt()
    clock = FakeClock()
    session, _ = make_session(art, clock)
    await session.async_start()
    assert await session.async_ensure_ready(ArtSessionTrigger.BACKGROUND)
    assert session.ready

    art.alive = False
    assert not session.ready
    session.observe_power(
        reachable=True, power_state="on", reachable_edge=False
    )
    await asyncio.sleep(0)

    assert session.state is ArtSessionState.BACKOFF
    assert session._next_retry_at == clock.now + 30
    assert art.start_listening.await_count == 1


async def test_stop_during_connect_is_terminal_and_closes_transport():
    art = FakeArt()
    art.block_next_start(ignore_cancellation=True)
    clock = FakeClock()
    session, _ = make_session(art, clock)
    await session.async_start()
    caller = asyncio.create_task(
        session.async_ensure_ready(ArtSessionTrigger.USER)
    )
    await wait_for_start_calls(art, 1)

    stop_task = asyncio.create_task(session.async_stop())
    await asyncio.sleep(0)
    art.release_start()
    await stop_task

    with pytest.raises(asyncio.CancelledError):
        await caller
    assert session.state is ArtSessionState.STOPPED
    assert art.stop.call_count == 1
    assert art.close.await_count == 1
    session.observe_power(
        reachable=True, power_state="on", reachable_edge=True
    )
    await asyncio.sleep(0)
    assert art.start_listening.await_count == 1


async def test_external_connect_cancellation_closes_and_remains_recoverable():
    art = FakeArt()
    art.block_next_start()
    clock = FakeClock()
    session, _ = make_session(art, clock)
    await session.async_start()
    caller = asyncio.create_task(
        session.async_ensure_ready(ArtSessionTrigger.BACKGROUND)
    )
    await wait_for_start_calls(art, 1)
    connect_task = session._connect_task
    assert connect_task is not None

    connect_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    assert art.close.await_count == 1
    assert session.state is ArtSessionState.BACKOFF
    assert session._next_retry_at == clock.now + 30
    clock.now = session._next_retry_at
    assert await session.async_ensure_ready(ArtSessionTrigger.BACKGROUND)
    assert session.state is ArtSessionState.READY


async def test_concurrent_stop_callers_wait_for_shared_shutdown():
    art = FakeArt()
    clock = FakeClock()
    session, _ = make_session(art, clock)
    await session.async_start()
    assert await session.async_ensure_ready(ArtSessionTrigger.BACKGROUND)
    art.block_next_close()

    first = asyncio.create_task(session.async_stop())
    await wait_for_close_calls(art, 1)
    second = asyncio.create_task(session.async_stop())
    await asyncio.sleep(0)

    assert not first.done()
    assert not second.done()
    art.release_close()
    await asyncio.gather(first, second)
    assert session.state is ArtSessionState.STOPPED
    assert not art.alive
    assert art.stop.call_count == 1
    assert art.close.await_count == 1


async def test_cancelling_one_stop_waiter_does_not_abandon_shared_shutdown():
    art = FakeArt()
    clock = FakeClock()
    session, _ = make_session(art, clock)
    await session.async_start()
    assert await session.async_ensure_ready(ArtSessionTrigger.BACKGROUND)
    art.block_next_close()

    cancelled_waiter = asyncio.create_task(session.async_stop())
    await wait_for_close_calls(art, 1)
    surviving_waiter = asyncio.create_task(session.async_stop())
    await asyncio.sleep(0)
    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter

    try:
        assert not surviving_waiter.done()
        stop_task = session._stop_task
        assert stop_task is not None
        assert not stop_task.done()
    finally:
        if art._close_gate is not None:
            art.release_close()
        await asyncio.gather(surviving_waiter, return_exceptions=True)
    assert session.state is ArtSessionState.STOPPED
    assert not art.alive
    assert art.stop.call_count == 1
    assert art.close.await_count == 1


async def test_ensure_waiter_cancellation_does_not_cancel_shared_connect():
    art = FakeArt()
    art.block_next_start()
    clock = FakeClock()
    session, _ = make_session(art, clock)
    await session.async_start()
    cancelled_waiter = asyncio.create_task(
        session.async_ensure_ready(ArtSessionTrigger.USER)
    )
    await wait_for_start_calls(art, 1)
    connect_task = session._connect_task
    assert connect_task is not None

    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter
    assert not connect_task.done()
    surviving_waiter = asyncio.create_task(
        session.async_ensure_ready(ArtSessionTrigger.USER)
    )

    art.release_start()
    assert await surviving_waiter
    assert session.state is ArtSessionState.READY
    assert art.start_listening.await_count == 1


async def test_stop_bounds_connect_that_ignores_cancellation(monkeypatch):
    shorten_lifecycle_deadlines(monkeypatch)
    art = FakeArt()
    art.block_next_start(ignore_cancellation=True)
    clock = FakeClock()
    session, _ = make_session(art, clock)
    await session.async_start()
    caller = asyncio.create_task(
        session.async_ensure_ready(ArtSessionTrigger.USER)
    )
    await wait_for_start_calls(art, 1)

    stop_waiter = asyncio.create_task(session.async_stop())
    try:
        await asyncio.wait_for(asyncio.shield(stop_waiter), 0.2)
    finally:
        if art._start_gate is not None:
            art.release_start()
        await asyncio.gather(stop_waiter, caller, return_exceptions=True)

    assert session.state is ArtSessionState.STOPPED
    assert session.generation == 0
    assert art.stop.call_count == 1
    assert not art.alive


async def test_stop_outer_deadline_bounds_blocked_transport_close(monkeypatch):
    shorten_lifecycle_deadlines(monkeypatch)
    art = FakeArt()
    clock = FakeClock()
    session, _ = make_session(art, clock)
    await session.async_start()
    assert await session.async_ensure_ready(ArtSessionTrigger.BACKGROUND)
    art.block_next_close()

    stop_waiter = asyncio.create_task(session.async_stop())
    try:
        await asyncio.wait_for(asyncio.shield(stop_waiter), 0.2)
    finally:
        if art._close_gate is not None:
            art.release_close()
        await asyncio.gather(stop_waiter, return_exceptions=True)

    assert session.state is ArtSessionState.STOPPED
    assert art.stop.call_count == 1
    assert art.close.await_count == 1
    assert not art.alive


async def test_cancelling_stop_owner_during_connect_wait_finishes_cleanup(
    monkeypatch,
):
    shorten_lifecycle_deadlines(monkeypatch)
    art = FakeArt()
    art.block_next_start(ignore_cancellation=True)
    clock = FakeClock()
    session, _ = make_session(art, clock)
    await session.async_start()
    caller = asyncio.create_task(
        session.async_ensure_ready(ArtSessionTrigger.USER)
    )
    await wait_for_start_calls(art, 1)
    stop_waiter = asyncio.create_task(session.async_stop())
    await wait_for_stop_calls(art, 1)
    owner = session._stop_task
    assert owner is not None
    assert not owner.done()

    owner.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(stop_waiter), 0.2)
    finally:
        if art._start_gate is not None:
            art.release_start()
        await asyncio.gather(stop_waiter, caller, return_exceptions=True)

    assert session.state is ArtSessionState.STOPPED
    assert art.stop.call_count == 1
    assert not art.alive
    await session.async_stop()


async def test_cancelling_stop_owner_during_close_does_not_poison_stop():
    art = FakeArt()
    clock = FakeClock()
    session, _ = make_session(art, clock)
    await session.async_start()
    assert await session.async_ensure_ready(ArtSessionTrigger.BACKGROUND)
    art.block_next_close()
    stop_waiter = asyncio.create_task(session.async_stop())
    await wait_for_close_calls(art, 1)
    owner = session._stop_task
    assert owner is not None

    owner.cancel()
    try:
        await asyncio.sleep(0)
        assert not owner.done()
    finally:
        if art._close_gate is not None:
            art.release_close()
        result = await asyncio.gather(stop_waiter, return_exceptions=True)

    assert result == [None]
    assert session.state is ArtSessionState.STOPPED
    assert art.stop.call_count == 1
    assert art.close.await_count == 1
    await session.async_stop()


async def test_cancelled_close_owner_is_replaced_before_stop_completes():
    art = FakeArt()
    clock = FakeClock()
    session, _ = make_session(art, clock)
    await session.async_start()
    assert await session.async_ensure_ready(ArtSessionTrigger.BACKGROUND)
    art.block_next_close()
    stop_waiter = asyncio.create_task(session.async_stop())
    await wait_for_close_calls(art, 1)
    close_owner = session._close_task
    assert close_owner is not None

    close_owner.cancel()
    try:
        await wait_for_close_calls(art, 2)
    finally:
        if art._close_gate is not None:
            art.release_close()
        result = await asyncio.gather(stop_waiter, return_exceptions=True)

    assert result == [None]
    assert session.state is ArtSessionState.STOPPED
    assert art.stop.call_count == 1
    assert art.close.await_count == 2
    await session.async_stop()
    assert art.close.await_count == 2


async def test_second_connect_cancellation_cannot_interrupt_close_recovery():
    art = FakeArt()
    art.block_next_start()
    art.block_next_close()
    clock = FakeClock()
    session, _ = make_session(art, clock)
    await session.async_start()
    caller = asyncio.create_task(
        session.async_ensure_ready(ArtSessionTrigger.BACKGROUND)
    )
    await wait_for_start_calls(art, 1)
    connect_task = session._connect_task
    assert connect_task is not None
    connect_task.cancel()
    await wait_for_close_calls(art, 1)

    try:
        connect_task.cancel()
        await asyncio.sleep(0)
        assert session.state is ArtSessionState.BACKOFF
        close_task = session._close_task
        assert close_task is not None
        assert not close_task.done()
    finally:
        if art._close_gate is not None:
            art.release_close()
        await asyncio.gather(caller, return_exceptions=True)

    assert art.close.await_count == 1
    assert session.state is ArtSessionState.BACKOFF


async def test_stop_joins_connect_cleanup_close_without_closing_twice():
    art = FakeArt()
    art.block_next_start()
    art.block_next_close()
    clock = FakeClock()
    session, factory = make_session(art, clock)
    await session.async_start()
    caller = asyncio.create_task(
        session.async_ensure_ready(ArtSessionTrigger.BACKGROUND)
    )
    await wait_for_start_calls(art, 1)
    connect_task = session._connect_task
    assert connect_task is not None
    connect_task.cancel()
    await wait_for_close_calls(art, 1)

    stop_waiter = asyncio.create_task(session.async_stop())
    await wait_for_stop_calls(art, 1)
    if art._close_gate is not None:
        art.release_close()
    await asyncio.gather(stop_waiter, caller, return_exceptions=True)

    assert session.state is ArtSessionState.STOPPED
    assert art.stop.call_count == 1
    assert art.close.await_count == 1
    task_names = [name for _coroutine, name in factory.calls]
    assert "samsungtv_frame-art-session-close" in task_names
    assert "samsungtv_frame-art-session-stop" in task_names


async def test_generic_failure_resets_consecutive_hostless_streak():
    art = FakeArt(
        [
            ArtHostUnavailable("no host"),
            ConnectionFailure("generic"),
            ArtHostUnavailable("no host"),
            ArtHostUnavailable("no host"),
        ]
    )
    clock = FakeClock()
    session, _ = make_session(art, clock)
    await session.async_start()

    for _ in range(4):
        assert not await session.async_ensure_ready(
            ArtSessionTrigger.BACKGROUND
        )
        clock.now = session._next_retry_at

    assert session.state is ArtSessionState.BACKOFF
    assert session._host_failure_count == 2


async def test_success_after_hostless_failure_resets_both_counters():
    art = FakeArt([ArtHostUnavailable("no host"), None])
    clock = FakeClock()
    session, _ = make_session(art, clock)
    await session.async_start()

    assert not await session.async_ensure_ready(ArtSessionTrigger.BACKGROUND)
    assert session._failure_count == 1
    assert session._host_failure_count == 1
    clock.now = session._next_retry_at
    assert await session.async_ensure_ready(ArtSessionTrigger.BACKGROUND)

    assert session.state is ArtSessionState.READY
    assert session._failure_count == 0
    assert session._host_failure_count == 0


async def test_state_callback_changes_without_replay_and_runs_once_per_change():
    art = FakeArt()
    clock = FakeClock()
    first_states: list[ArtSessionState] = []
    second_states: list[ArtSessionState] = []
    session, _ = make_session(
        art, clock, state_callback=first_states.append
    )

    await session.async_start()
    session.set_state_callback(second_states.append)
    assert first_states == [ArtSessionState.BACKOFF]
    assert second_states == []

    assert await session.async_ensure_ready(ArtSessionTrigger.BACKGROUND)
    assert await session.async_ensure_ready(ArtSessionTrigger.BACKGROUND)
    assert second_states == [
        ArtSessionState.CONNECTING,
        ArtSessionState.READY,
    ]


async def test_state_callback_failure_does_not_corrupt_session(caplog):
    art = FakeArt()
    clock = FakeClock()

    def broken_callback(_state: ArtSessionState) -> None:
        raise RuntimeError("callback failed")

    session, _ = make_session(
        art, clock, state_callback=broken_callback
    )

    await session.async_start()
    assert await session.async_ensure_ready(ArtSessionTrigger.BACKGROUND)

    assert session.state is ArtSessionState.READY
    assert session.ready
    assert "Art session state callback failed" in caplog.text
