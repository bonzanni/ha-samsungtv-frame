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
