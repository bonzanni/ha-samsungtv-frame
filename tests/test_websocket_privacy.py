"""Tests for websocket protocol parsing without credential logging."""
from __future__ import annotations

import logging

import pytest
from samsungtvws.exceptions import ResponseError

from custom_components.samsung_tv_frame.websocket_privacy import (
    QUIET_WEBSOCKET_LOGGER,
    process_api_response_silently,
)


def test_quiet_websocket_logger_never_emits_to_root(caplog):
    secret = "private-websocket-url-token"
    caplog.set_level(logging.DEBUG)

    QUIET_WEBSOCKET_LOGGER.debug("handshake URL contains %s", secret)

    assert secret not in caplog.text


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ('{"event":"ready","value":1}', {"event": "ready", "value": 1}),
        (b'{"event":"ready","value":2}', {"event": "ready", "value": 2}),
        (
            b'prefix{"event":"ready","value":3}\n\xff\xd8private-binary-tail',
            {
                "event": "ready",
                "value": 3,
                "binary": b"\xff\xd8private-binary-tail",
                "binary_len": 21,
            },
        ),
    ],
)
def test_silent_response_parser_matches_supported_upstream_frames(
    response, expected
):
    assert process_api_response_silently(response) == expected


@pytest.mark.parametrize(
    "response",
    [
        "private-invalid-json",
        b"private-no-json-start",
        b'prefix{"private":"unclosed"',
        b'prefix{"private": invalid}',
    ],
)
def test_silent_response_parser_raises_fixed_error_without_private_cause(
    response,
):
    with pytest.raises(ResponseError) as raised:
        process_api_response_silently(response)

    assert str(raised.value) == "Failed to parse response from TV"
    assert raised.value.__suppress_context__ is True
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "private" not in str(raised.value)
