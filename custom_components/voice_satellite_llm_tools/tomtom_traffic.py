"""TomTom traffic provider."""

from __future__ import annotations

from urllib.parse import quote

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .base_traffic import BaseTrafficTool
from .const import CONF_TOMTOM_API_KEY

TOMTOM_SEARCH_URL = "https://api.tomtom.com/search/2/search"
TOMTOM_ROUTING_URL = "https://api.tomtom.com/routing/1/calculateRoute"
REQUEST_TIMEOUT = 20


class TomTomTrafficTool(BaseTrafficTool):
    """Traffic conditions and drive time from TomTom."""

    source = "tomtom"
    provider = "TomTom"

    @classmethod
    def is_configured(cls, config: dict) -> bool:
        """TomTom needs an API key before the tool can be exposed."""
        return bool(config.get(CONF_TOMTOM_API_KEY, "").strip())

    async def async_resolve_place(
        self,
        hass: HomeAssistant,
        query: str,
        display_name: str | None = None,
    ) -> dict:
        """Turn an address or place name into coordinates with TomTom Search."""
        coordinates = self._parse_coordinates(query)
        if coordinates:
            return {
                "name": display_name or query,
                "lat": coordinates[0],
                "lon": coordinates[1],
            }

        api_key = self._api_key()
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

    async def async_get_route(
        self, hass: HomeAssistant, origin: dict, destination: dict
    ) -> dict:
        """Ask TomTom for the fastest route with live traffic."""
        session = async_get_clientsession(hass)
        locations = (
            f"{origin['lat']},{origin['lon']}:"
            f"{destination['lat']},{destination['lon']}"
        )
        url = f"{TOMTOM_ROUTING_URL}/{locations}/json"
        params = [
            ("key", self._api_key()),
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

    def _api_key(self) -> str:
        """Return the configured TomTom key or fail with a clear error."""
        api_key = self.config.get(CONF_TOMTOM_API_KEY, "").strip()
        if not api_key:
            raise RuntimeError("TomTom API key not configured.")
        return api_key

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
