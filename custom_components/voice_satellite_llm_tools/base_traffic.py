"""Base traffic tool and the provider contract.

A traffic provider implements two methods:

``async_resolve_place``
    Turn a spoken saved name, address, or place name into coordinates.

``async_get_route``
    Return a normalized route.  The card and the spoken summary only
    depend on this small shape, so a new provider does not need to touch
    the card:

    {
        "summary": {
            "travelTimeInSeconds": int,
            "trafficDelayInSeconds": int,
            "noTrafficTravelTimeInSeconds": int,
            "lengthInMeters": float,
        },
        "sections": [
            {
                "simpleCategory": "JAM" | "ROAD_WORK" | "ROAD_CLOSURE" | "OTHER",
                "magnitudeOfDelay": 0-4,
                "delayInSeconds": int,
                "eventId": str,
            },
            {
                "sectionType": "IMPORTANT_ROAD_STRETCH",
                "importantRoadStretchIndex": int,
                "roadNumbers": [{"text": "US-50 W"}],
                "streetName": {"text": "..."},
            },
        ],
    }

Providers can supply their own preview URL; the default is a Google
Maps directions link built from the resolved coordinates.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from urllib.parse import urlencode

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.util import dt as dt_util

from .base_tool import BaseTool
from .const import (
    CONF_TRAFFIC_DESTINATIONS,
    CONF_TRAFFIC_MAX_INCIDENTS,
    CONF_TRAFFIC_ORIGIN,
    DEFAULT_TRAFFIC_MAX_INCIDENTS,
)
from .traffic_utils import build_traffic_payload, match_saved_destination

_LOGGER = logging.getLogger(__name__)


class BaseTrafficTool(BaseTool):
    """Provider-independent traffic tool."""

    source = "traffic"
    name = "get_traffic"
    description = (
        "Get current traffic conditions and driving time for a route. "
        "Use this when the user asks how traffic is, how long it will take "
        "to get somewhere, whether there is a delay, or about accidents, "
        "road work, or closures on the way. Pass the destination as a saved "
        "place name (for example 'work') or as an address or place name."
    )

    parameters = vol.Schema(
        {
            vol.Required(
                "destination",
                description=(
                    "Where the user is driving to. A saved destination name "
                    "(for example 'work'), or an address or place name."
                ),
            ): str,
            vol.Optional(
                "origin",
                description=(
                    "Where the drive starts. Defaults to home. Use only when "
                    "the user says they are starting somewhere else."
                ),
            ): str,
        }
    )

    @classmethod
    def is_configured(cls, config: dict) -> bool:
        """Return True when the provider has the configuration it needs."""
        return True

    @abstractmethod
    async def async_resolve_place(
        self,
        hass: HomeAssistant,
        query: str,
        display_name: str | None = None,
    ) -> dict:
        """Return {"name", "lat", "lon"} for a place."""

    @abstractmethod
    async def async_get_route(
        self, hass: HomeAssistant, origin: dict, destination: dict
    ) -> dict:
        """Return a normalized route (see the module docstring)."""

    def preview_url(self, origin: dict, destination: dict) -> str:
        """Return a link opened by the card's Preview action."""
        query = urlencode(
            {
                "api": "1",
                "origin": f"{origin['lat']},{origin['lon']}",
                "destination": f"{destination['lat']},{destination['lon']}",
                "travelmode": "driving",
            }
        )
        return f"https://www.google.com/maps/dir/?{query}"

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> dict:
        """Execute the traffic tool."""
        destination_query = (
            tool_input.tool_args.get("destination", "").strip()
        )
        origin_query = tool_input.tool_args.get("origin", "").strip()

        if not destination_query:
            return {"error": "A destination is required."}

        try:
            origin = await self._resolve_origin(hass, origin_query)
            destination = await self._resolve_destination(
                hass, destination_query
            )

            if (
                abs(origin["lat"] - destination["lat"]) < 0.0001
                and abs(origin["lon"] - destination["lon"]) < 0.0001
            ):
                return {
                    "error": (
                        "The origin and destination are the same place. "
                        "Ask the user where they are driving to."
                    )
                }

            route = await self.async_get_route(hass, origin, destination)
            return self._build_response(route, origin, destination)

        except Exception as e:
            _LOGGER.exception("Traffic lookup error: %s", e)
            return {"error": f"Traffic lookup failed: {e!s}"}

    async def _resolve_origin(
        self, hass: HomeAssistant, origin_query: str
    ) -> dict:
        """Resolve the start of the drive.

        Precedence: an explicit tool argument, the configured origin, and
        finally the Home Assistant home coordinates.
        """
        query = (
            origin_query
            or self.config.get(CONF_TRAFFIC_ORIGIN, "").strip()
        )
        if query:
            saved = match_saved_destination(
                self.config.get(CONF_TRAFFIC_DESTINATIONS, ""), query
            )
            if saved:
                name, address = saved
                return await self.async_resolve_place(hass, address, name)
            return await self.async_resolve_place(hass, query)

        latitude = hass.config.latitude
        longitude = hass.config.longitude
        if latitude is None or longitude is None:
            raise RuntimeError(
                "Home coordinates are not configured. Set an origin address "
                "in the traffic tool options."
            )
        return {"name": "Home", "lat": latitude, "lon": longitude}

    async def _resolve_destination(
        self, hass: HomeAssistant, query: str
    ) -> dict:
        """Resolve a saved destination name or a free-text place."""
        saved = match_saved_destination(
            self.config.get(CONF_TRAFFIC_DESTINATIONS, ""), query
        )
        if saved:
            name, address = saved
            return await self.async_resolve_place(hass, address, name)

        return await self.async_resolve_place(hass, query)

    def _build_response(
        self, route: dict, origin: dict, destination: dict
    ) -> dict:
        """Shape a normalized route into the card and LLM payload."""
        return build_traffic_payload(
            route.get("summary") or {},
            route.get("sections") or [],
            origin["name"],
            destination["name"],
            self._uses_imperial_units(),
            self._max_incidents(),
            dt_util.now(),
            self.preview_url(origin, destination),
        )

    def _uses_imperial_units(self) -> bool:
        """Return True when Home Assistant is configured for miles."""
        units = getattr(self.hass.config, "units", None)
        length_unit = getattr(units, "length_unit", "")
        return length_unit in ("mi", "yd", "ft", "in")

    def _max_incidents(self) -> int:
        """Return the configured incident cap, with a safe fallback."""
        try:
            return int(
                self.config.get(
                    CONF_TRAFFIC_MAX_INCIDENTS, DEFAULT_TRAFFIC_MAX_INCIDENTS
                )
            )
        except (TypeError, ValueError):
            return DEFAULT_TRAFFIC_MAX_INCIDENTS
