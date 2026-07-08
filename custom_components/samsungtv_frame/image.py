"""Image entity showing the artwork currently displayed on the Frame."""
from __future__ import annotations

from pathlib import Path

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CONF_MAC
from .coordinator import FrameConfigEntry, FrameCoordinator
from .entity import FrameEntity

PARALLEL_UPDATES = 0

# Shown when the TV refuses the thumbnail (Samsung Store artworks are
# DRM-protected) — a designed card beats the frontend's broken-image icon.
_PLACEHOLDER_PATH = Path(__file__).parent / "placeholder.jpg"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FrameConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([FrameCurrentArtImage(entry.runtime_data, hass)])


class FrameCurrentArtImage(FrameEntity, ImageEntity):
    """Thumbnail of the current artwork, refreshed when the selection changes."""

    _attr_translation_key = "current_art_image"
    _attr_name = "Current art image"
    _attr_content_type = "image/jpeg"

    def __init__(self, coordinator: FrameCoordinator, hass: HomeAssistant) -> None:
        FrameEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, hass)
        self._attr_unique_id = (
            f"{coordinator.config_entry.data[CONF_MAC]}_current_art_image"
        )
        self._fetched_id: str | None = None
        self._fetched_image: bytes | None = None
        self._placeholder: bytes | None = None
        if coordinator.data and coordinator.data.current_art:
            self._attr_image_last_updated = dt_util.utcnow()

    @callback
    def _handle_coordinator_update(self) -> None:
        content_id = self.coordinator.data.current_art
        # Bump the timestamp only on a real change: the frontend refetches
        # the proxied image whenever image_last_updated moves.
        if content_id and content_id != self._fetched_id:
            self._attr_image_last_updated = dt_util.utcnow()
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        content_id = self.coordinator.data.current_art
        if content_id is None:
            return self._fetched_image or await self._async_placeholder()
        if content_id == self._fetched_id and self._fetched_image is not None:
            return self._fetched_image
        image = await self.coordinator.device.async_get_art_thumbnail(content_id)
        if image is not None:
            self._fetched_id = content_id
            self._fetched_image = image
        # Serve the previous artwork rather than a broken image if the
        # thumbnail fetch fails (TV off, or DRM-refused store artwork);
        # fall back to the bundled placeholder when nothing was ever fetched.
        return image or self._fetched_image or await self._async_placeholder()

    async def _async_placeholder(self) -> bytes:
        if self._placeholder is None:
            self._placeholder = await self.hass.async_add_executor_job(
                _PLACEHOLDER_PATH.read_bytes
            )
        return self._placeholder
