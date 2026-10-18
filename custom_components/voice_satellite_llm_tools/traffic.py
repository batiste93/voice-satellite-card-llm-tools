"""Traffic conditions and drive time using the TomTom Routing API."""

from __future__ import annotations

import logging
from urllib.parse import quote, urlencode

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .base_tool import BaseTool
from .const import (
    CONF_TOMTOM_API_KEY,
    CONF_TRAFFIC_DESTINATIONS,
    CONF_TRAFFIC_MAX_INCIDENTS,
    CONF_TRAFFIC_ORIGIN,
    DEFAULT_TRAFFIC_MAX_INCIDENTS,
)
from .traffic_utils import build_traffic_payload, match_saved_destination

_LOGGER = logging.getLogger(__name__)

TOMTOM_SEARCH_URL = "https://api.tomtom.com/search/2/search"
TOMTOM_ROUTING_URL = "https://api.tomtom.com/routing/1/calculateRoute"
REQUEST_TIMEOUT = 20


class TrafficTool(BaseTool):
    """Get current traffic conditions and travel time for a drive."""

    source = "tomtom"
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

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> dict:
        """Execute the traffic tool."""
        api_key = self.config.get(CONF_TOMTOM_API_KEY, "").strip()
        if not api_key:
            return {"error": "TomTom API key not configured."}

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

            route = await self._calculate_route(
                hass, api_key, origin, destination
            )
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
        query = origin_query or self.config.get(CONF_TRAFFIC_ORIGIN, "").strip()
        if query:
            saved = match_saved_destination(
                self.config.get(CONF_TRAFFIC_DESTINATIONS, ""), query
            )
            if saved:
                name, address = saved
                return await self._geocode(hass, address, name)
            return await self._geocode(hass, query)

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
        """Resolve a saved destination name or geocode a free-text place."""
        saved = match_saved_destination(
            self.config.get(CONF_TRAFFIC_DESTINATIONS, ""), query
        )
        if saved:
            name, address = saved
            return await self._geocode(hass, address, name)

        return await self._geocode(hass, query)

    async def _geocode(
        self, hass: HomeAssistant, query: str, display_name: str | None = None
    ) -> dict:
        """Turn an address or place name into coordinates."""
        coordinates = self._parse_coordinates(query)
        if coordinates:
            return {
                "name": display_name or query,
                "lat": coordinates[0],
                "lon": coordinates[1],
            }

        api_key = self.config.get(CONF_TOMTOM_API_KEY, "").strip()
        session = async_get_clientsession(hass)
        params = {
            "key": api_key,
            "limit": 1,
            "typeahead": "false",
            "language": "en-GB",
        }
        if hass.config.latitude is not None and hass.config.longitude is not None:
            params["lat"] = hass.config.latitude
            params["lon"] = hass.config.longitude
        url = f"{TOMTOM_SEARCH_URL}/{quote(query, safe='')}.json"

        async with session.get(
            url, params=params, timeout=REQUEST_TIMEOUT
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise RuntimeError(
                    self._http_error("search", resp.status, body)
                )
            data = await resp.json()

        results = data.get("results") or []
        if not results:
            raise RuntimeError(
                f"I couldn't find '{query}'. Try a full street address, or "
                "save the place in the traffic tool options."
            )

        result = results[0]
        position = result.get("position") or {}
        latitude = position.get("lat")
        longitude = position.get("lon")
        if latitude is None or longitude is None:
            raise RuntimeError(f"I couldn't find coordinates for '{query}'.")

        address = (result.get("address") or {}).get("freeformAddress")
        poi_name = (result.get("poi") or {}).get("name")
        name = display_name or poi_name or address or query

        return {
            "name": name,
            "lat": float(latitude),
            "lon": float(longitude),
        }

    async def _calculate_route(
        self,
        hass: HomeAssistant,
        api_key: str,
        origin: dict,
        destination: dict,
    ) -> dict:
        """Ask TomTom for the fastest route with live traffic."""
        session = async_get_clientsession(hass)
        locations = (
            f"{origin['lat']},{origin['lon']}:"
            f"{destination['lat']},{destination['lon']}"
        )
        url = f"{TOMTOM_ROUTING_URL}/{locations}/json"
        params = [
            ("key", api_key),
            ("traffic", "true"),
            ("travelMode", "car"),
            ("routeRepresentation", "summaryOnly"),
            ("computeTravelTimeFor", "all"),
            ("sectionType", "traffic"),
            ("sectionType", "importantRoadStretch"),
            ("language", "en-GB"),
        ]

        async with session.get(
            url, params=params, timeout=REQUEST_TIMEOUT
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise RuntimeError(
                    self._http_error("route", resp.status, body)
                )
            data = await resp.json()

        routes = data.get("routes") or []
        if not routes:
            raise RuntimeError(
                "TomTom could not calculate a route between those places."
            )
        return routes[0]

    @staticmethod
    def _parse_coordinates(query: str) -> tuple[float, float] | None:
        """Accept a literal ``lat,lon`` pair for testing or explicit use."""
        parts = query.split(",")
        if len(parts) != 2:
            return None
        try:
            latitude = float(parts[0])
            longitude = float(parts[1])
        except ValueError:
            return None
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            return None
        return latitude, longitude

    @staticmethod
    def _http_error(service: str, status: int, body: str) -> str:
        """Translate a TomTom HTTP failure into a useful tool error."""
        detail = (body or "").strip()
        if len(detail) > 300:
            detail = detail[:300] + "..."
        if status in (401, 403):
            return (
                "TomTom rejected the API key. Check the key in the traffic "
                "tool options and that the Routing and Search APIs are enabled."
            )
        if status == 429:
            return "TomTom rate limit reached. Try again in a moment."
        if status == 400:
            return f"TomTom could not process the {service} request. {detail}"
        return f"TomTom {service} returned HTTP {status}. {detail}"

    def _build_response(
        self, route: dict, origin: dict, destination: dict
    ) -> dict:
        """Shape a TomTom route into the card and LLM payload."""
        return build_traffic_payload(
            route.get("summary") or {},
            route.get("sections") or [],
            origin["name"],
            destination["name"],
            self._uses_imperial_units(),
            self._max_incidents(),
            dt_util.now(),
            self._map_url(origin, destination),
        )

    @staticmethod
    def _map_url(origin: dict, destination: dict) -> str:
        """Build a maps preview URL from the resolved coordinates."""
        query = urlencode(
            {
                "api": "1",
                "origin": f"{origin['lat']},{origin['lon']}",
                "destination": f"{destination['lat']},{destination['lon']}",
                "travelmode": "driving",
            }
        )
        return f"https://www.google.com/maps/dir/?{query}"

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
