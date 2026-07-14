"""Privacy-safe websocket logging and response parsing helpers."""
from __future__ import annotations

import json
import logging
from typing import Any

from samsungtvws.exceptions import ResponseError


class _QuietWebSocketLogger(logging.Logger):
    """A connection-specific logger that can never emit handshake details."""

    def isEnabledFor(self, level: int) -> bool:  # noqa: N802 - logging API
        return False

    def handle(self, record: logging.LogRecord) -> None:
        return None

    def _log(self, *args, **kwargs) -> None:
        return None


QUIET_WEBSOCKET_LOGGER = _QuietWebSocketLogger(
    f"{__name__}.quiet_websocket"
)

_SANITIZED_RESPONSE_ERROR = "Failed to parse response from TV"


def _split_json_and_tail(response: bytes) -> tuple[bytes, bytes]:
    """Split the first brace-balanced JSON object from a binary tail."""
    start = response.find(b"{")
    if start < 0:
        raise ValueError

    depth = 0
    end = None
    for position in range(start, len(response)):
        byte = response[position]
        if byte == ord("{"):
            depth += 1
        elif byte == ord("}"):
            depth -= 1
            if depth == 0:
                end = position + 1
                break
    if end is None:
        raise ValueError

    tail = response[end:]
    if tail.startswith(b"\n"):
        tail = tail[1:]
    return response[start:end], tail


def process_api_response_silently(response: str | bytes) -> Any:
    """Match samsungtvws response decoding without logging private frames."""
    try:
        if isinstance(response, str):
            return json.loads(response)
        if not isinstance(response, bytes):
            raise TypeError
        try:
            return json.loads(response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            json_bytes, tail = _split_json_and_tail(response)
            frame = json.loads(json_bytes.decode("utf-8"))
            if tail:
                frame["binary"] = tail
                frame["binary_len"] = len(tail)
            return frame
    except (
        AttributeError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ):
        pass
    raise ResponseError(_SANITIZED_RESPONSE_ERROR) from None
