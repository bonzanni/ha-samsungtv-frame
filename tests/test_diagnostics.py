"""Privacy and zero-I/O tests for config-entry diagnostics."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.samsung_tv_frame.art_session import ArtSessionState
from custom_components.samsung_tv_frame.const import (
    CONF_HOST,
    CONF_MAC,
    CONF_MODEL,
    CONF_TOKEN,
    DOMAIN,
    OPT_HEARTBEAT,
)
from custom_components.samsung_tv_frame.coordinator import FrameCoordinator
from custom_components.samsung_tv_frame.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.samsung_tv_frame.models import (
    ArtSettingKey,
    ArtSettingsSnapshot,
    FrameData,
    SlideshowMode,
    SlideshowState,
    TvMode,
)


LOADED_PRIVATE_CANARIES = (
    "__DIAG_LOADED_HOST_CANARY__",
    "__DIAG_LOADED_MAC_CANARY__",
    "__DIAG_LOADED_TOKEN_CANARY__",
    "__DIAG_LOADED_TITLE_CANARY__",
    "__DIAG_LOADED_ENTRY_IDENTIFIER_CANARY__",
    "__DIAG_LOADED_DATA_KEY_CANARY__",
    "__DIAG_LOADED_DATA_VALUE_CANARY__",
    "__DIAG_LOADED_OPTION_KEY_CANARY__",
    "__DIAG_LOADED_OPTION_VALUE_CANARY__",
    "__DIAG_LOADED_CURRENT_ART_CANARY__",
    "__DIAG_LOADED_RUNNING_APP_CANARY__",
    "__DIAG_LOADED_SLIDESHOW_CATEGORY_CANARY__",
    "__DIAG_LOADED_SETTING_VALUE_CANARY__",
    "__DIAG_LOADED_COORDINATOR_PRIVATE_CANARY__",
    "__DIAG_LOADED_DEVICE_PRIVATE_CANARY__",
)

UNLOADED_PRIVATE_CANARIES = (
    "__DIAG_UNLOADED_HOST_CANARY__",
    "__DIAG_UNLOADED_MAC_CANARY__",
    "__DIAG_UNLOADED_TOKEN_CANARY__",
    "__DIAG_UNLOADED_TITLE_CANARY__",
    "__DIAG_UNLOADED_ENTRY_IDENTIFIER_CANARY__",
    "__DIAG_UNLOADED_DATA_KEY_CANARY__",
    "__DIAG_UNLOADED_DATA_VALUE_CANARY__",
    "__DIAG_UNLOADED_OPTION_KEY_CANARY__",
    "__DIAG_UNLOADED_OPTION_VALUE_CANARY__",
)


def _forbid_all_async_device_methods(mock_device) -> dict[str, AsyncMock]:
    """Make every async method exposed by the fixture fail if used."""
    methods: dict[str, AsyncMock] = {}
    for name in dir(mock_device):
        value = getattr(mock_device, name)
        if isinstance(value, AsyncMock):
            methods[name] = value
    assert methods
    for name, method in methods.items():
        method.side_effect = AssertionError(
            f"diagnostics unexpectedly used device method {name}"
        )
    return methods


def _assert_device_methods_unused(
    mock_device, async_methods: dict[str, AsyncMock]
) -> None:
    """Prove diagnostics performed neither async nor sync device calls."""
    for method in async_methods.values():
        method.assert_not_called()
        method.assert_not_awaited()
    for value in vars(mock_device).values():
        if isinstance(value, MagicMock):
            value.assert_not_called()


async def test_loaded_diagnostics_exact_allowlist_privacy_and_zero_io(
    hass, mock_device
):
    (
        host_canary,
        mac_canary,
        token_canary,
        title_canary,
        entry_identifier_canary,
        data_key_canary,
        data_value_canary,
        option_key_canary,
        option_value_canary,
        current_art_canary,
        running_app_canary,
        slideshow_category_canary,
        setting_value_canary,
        coordinator_private_canary,
        device_private_canary,
    ) = LOADED_PRIVATE_CANARIES
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title_canary,
        data={
            CONF_HOST: host_canary,
            CONF_MAC: mac_canary,
            CONF_TOKEN: token_canary,
            CONF_MODEL: "QE65LS03BAUXXH",
            data_key_canary: data_value_canary,
        },
        options={
            OPT_HEARTBEAT: 37,
            option_key_canary: option_value_canary,
        },
        unique_id=entry_identifier_canary,
    )
    entry.add_to_hass(hass)
    coordinator = FrameCoordinator(hass, entry, mock_device)
    coordinator.last_update_success = False
    coordinator._art_fail_streak = 2
    coordinator._upnp_fail_streak = 3
    coordinator._unreachable_count = 4
    coordinator._art_implies_power_on = False
    coordinator._diagnostics_private_canary = coordinator_private_canary
    coordinator.data = FrameData(
        reachable=True,
        power_state="on",
        art_mode=False,
        tv_mode=TvMode.WATCHING,
        current_art=current_art_canary,
        running_app=running_app_canary,
        volume_level=0.73,
        is_muted=True,
        art_settings=ArtSettingsSnapshot(
            supported=frozenset(
                {
                    ArtSettingKey.MOTION_SENSITIVITY,
                    ArtSettingKey.COLOR_TEMPERATURE,
                }
            ),
            color_temperature=3,
            motion_sensitivity=setting_value_canary,
        ),
        slideshow=SlideshowState(
            mode=SlideshowMode.SHUFFLE,
            duration_minutes=60,
            category_id=slideshow_category_canary,
        ),
        optional_art_generation=12,
    )
    mock_device.art_generation = 12
    mock_device.art_ready = True
    mock_device.art_session_state = ArtSessionState.READY
    mock_device.remote_confirmed = True
    mock_device._diagnostics_private_canary = device_private_canary
    entry.runtime_data = coordinator
    async_methods = _forbid_all_async_device_methods(mock_device)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result == {
        "loaded": True,
        "model": "QE65LS03BAUXXH",
        "heartbeat_seconds": 37,
        "last_update_success": False,
        "reachable": True,
        "power_state": "on",
        "tv_mode": "watching",
        "art_mode_known": True,
        "art_session_state": "ready",
        "art_session_ready": True,
        "art_session_generation": 12,
        "art_failures": 2,
        "upnp_failures": 3,
        "unreachable_failures": 4,
        "standby_precedence_learned": False,
        "supported_settings": [
            "color_temperature",
            "motion_sensitivity",
        ],
        "slideshow_known": True,
        "remote_confirmed": True,
    }
    serialized = json.dumps(result, sort_keys=True)
    for canary in LOADED_PRIVATE_CANARIES:
        assert canary not in serialized
    _assert_device_methods_unused(mock_device, async_methods)


async def test_unloaded_diagnostics_exact_static_allowlist(hass):
    (
        host_canary,
        mac_canary,
        token_canary,
        title_canary,
        entry_identifier_canary,
        data_key_canary,
        data_value_canary,
        option_key_canary,
        option_value_canary,
    ) = UNLOADED_PRIVATE_CANARIES
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title_canary,
        data={
            CONF_HOST: host_canary,
            CONF_MAC: mac_canary,
            CONF_TOKEN: token_canary,
            CONF_MODEL: "QE65LS03BAUXXH",
            data_key_canary: data_value_canary,
        },
        options={option_key_canary: option_value_canary},
        unique_id=entry_identifier_canary,
    )
    entry.add_to_hass(hass)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result == {
        "loaded": False,
        "model": "QE65LS03BAUXXH",
        "heartbeat_seconds": 10,
    }
    serialized = json.dumps(result, sort_keys=True)
    for canary in UNLOADED_PRIVATE_CANARIES:
        assert canary not in serialized
