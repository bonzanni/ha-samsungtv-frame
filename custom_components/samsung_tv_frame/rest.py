"""Privacy-safe async REST transport for Samsung Frame TVs."""
from __future__ import annotations

from typing import Any, Literal, cast

import aiohttp
from samsungtvws.async_rest import SamsungTVAsyncRest
from samsungtvws.exceptions import HttpApiError

from .const import LOGGER
from .websocket_privacy import process_api_response_silently

_RequestMethod = Literal["GET", "POST", "PUT", "DELETE"]


class PrivacySafeSamsungTVAsyncRest(SamsungTVAsyncRest):
    """Preserve upstream REST behavior without dependency payload logging."""

    async def _rest_request(
        self, method: _RequestMethod, target: str
    ) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(self.timeout)
        url = self._format_rest_url(target)
        try:
            future = self.session.request(
                method, url, timeout=timeout, ssl=False
            )
            async with future as response:
                return cast(
                    dict[str, Any],
                    process_api_response_silently(await response.text()),
                )
        except aiohttp.ClientConnectionError as err:
            raise HttpApiError(
                "TV unreachable or feature not supported on this model."
            ) from err

    async def rest_device_info(self) -> dict[str, Any]:
        """Return device information without logging its response."""
        LOGGER.debug("Get device info via REST API")
        return await self._rest_request("GET", "")

    async def rest_app_status(self, app_id: str) -> dict[str, Any]:
        """Return app status without logging its identifier or response."""
        LOGGER.debug("Get app status via REST API")
        return await self._rest_request("GET", "applications/" + app_id)
