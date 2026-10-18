"""Pure helpers for the traffic tool.

Kept free of Home Assistant imports so the parsing and formatting rules
can be tested without a running Home Assistant instance.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# Traffic incident categories returned by the TomTom Routing API when
# ``sectionType=traffic`` is requested.
INCIDENT_TITLES = {
    "JAM": "Traffic jam",
    "ROAD_WORK": "Road work",
    "ROAD_CLOSURE": "Road closure",
    "OTHER": "Incident",
}

# TomTom's magnitudeOfDelay: 0 unknown, 1 minor, 2 moderate, 3 major,
# 4 undefined (used for closures and other indefinite delays).
SEVERITIES_BY_MAGNITUDE = {
    1: "minor",
    2: "moderate",
    3: "major",
    4: "closure",
}

SEVERITY_DESCRIPTIONS = {
    "minor": "Minor delay",
    "moderate": "Moderate delay",
    "major": "Major delay",
    "closure": "Long delay",
}

TRAFFIC_LEVELS = ("light", "moderate", "heavy", "severe")


def parse_saved_destinations(raw: str) -> dict[str, tuple[str, str]]:
    """Parse the ``Name = address`` lines from the config option.

    Returns a dict keyed by the case-folded name.  A line without a
    separator is treated as an address whose name is the address itself,
    so a user can paste a list of plain addresses.
    """
    destinations: dict[str, tuple[str, str]] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        name = line
        address = line
        for separator in ("=", ":"):
            if separator in line:
                candidate_name, candidate_address = line.split(separator, 1)
                candidate_name = candidate_name.strip()
                candidate_address = candidate_address.strip()
                if candidate_name and candidate_address:
                    name, address = candidate_name, candidate_address
                break

        destinations[name.casefold()] = (name, address)
    return destinations


def match_saved_destination(
    raw: str, query: str
) -> tuple[str, str] | None:
    """Look up a spoken destination name in the saved destinations."""
    if not query:
        return None
    return parse_saved_destinations(raw).get(query.strip().casefold())


def format_distance(meters: float, imperial: bool) -> str:
    """Format a route length for the spoken answer."""
    if meters is None or meters < 0:
        return ""
    if imperial:
        return f"{meters / 1609.344:.1f} mi"
    return f"{meters / 1000:.1f} km"


def format_distance_long(meters: float, imperial: bool) -> str:
    """Format a route length for the directions card."""
    if meters is None or meters < 0:
        return ""
    if imperial:
        value = meters / 1609.344
        unit = "mile" if abs(value - 1) < 0.05 else "miles"
    else:
        value = meters / 1000
        unit = "kilometer" if abs(value - 1) < 0.05 else "kilometers"
    return f"{value:.1f} {unit}"


def extract_via(sections: list[dict]) -> str:
    """Pick the most important road name TomTom reports for the route."""
    stretches = [
        section
        for section in sections or []
        if section.get("sectionType") == "IMPORTANT_ROAD_STRETCH"
    ]
    stretches.sort(
        key=lambda section: section.get("importantRoadStretchIndex", 999)
    )
    for stretch in stretches:
        for road in stretch.get("roadNumbers") or []:
            text = (road or {}).get("text")
            if text:
                return text
        text = (stretch.get("streetName") or {}).get("text")
        if text:
            return text
    return ""


def route_description(incidents: list[dict], delay_minutes: int) -> str:
    """Write the short recommendation line under the route title."""
    if incidents:
        if len(incidents) == 1:
            return f"Best route now, {incidents[0]['title'].lower()} on the way."
        return f"Best route now, {len(incidents)} incidents on the way."
    if delay_minutes >= 1:
        return f"Best route now, about {delay_minutes} min longer than usual."
    return "Best route now, light traffic."


def classify_traffic(delay_seconds: float, free_flow_seconds: float) -> str:
    """Turn a delay into a coarse traffic level for the card.

    A route with a couple of minutes of delay is normal city driving and
    reads as light even if it is a large share of a very short trip.
    """
    delay_seconds = max(0.0, float(delay_seconds or 0))
    free_flow_seconds = max(0.0, float(free_flow_seconds or 0))

    if delay_seconds <= 120:
        return "light"

    ratio = delay_seconds / free_flow_seconds if free_flow_seconds else 0.0
    if ratio <= 0.10:
        return "light"
    if ratio <= 0.25:
        return "moderate"
    if ratio <= 0.50:
        return "heavy"
    return "severe"


def incident_title(category: str | None) -> str:
    """Return the human title for a TomTom ``simpleCategory``."""
    return INCIDENT_TITLES.get((category or "").upper(), "Incident")


def incident_severity(magnitude: int | None) -> str:
    """Return a card CSS severity for a TomTom ``magnitudeOfDelay``."""
    try:
        magnitude = int(magnitude)
    except (TypeError, ValueError):
        magnitude = 0
    return SEVERITIES_BY_MAGNITUDE.get(magnitude, "minor")


def severity_description(severity: str) -> str:
    """Return the short description shown under an incident title."""
    return SEVERITY_DESCRIPTIONS.get(severity, "")


def format_arrival_time(arrival: datetime) -> str:
    """Format an arrival time the way the card shows it (4:32 PM)."""
    return arrival.strftime("%I:%M %p").lstrip("0")


def collect_incidents(sections: list[dict], limit: int) -> list[dict]:
    """Extract, de-duplicate, and rank the traffic sections on a route."""
    if limit <= 0:
        return []

    incidents: list[dict] = []
    seen: set[str] = set()
    for section in sections or []:
        category = section.get("simpleCategory")
        if not category and not section.get("tec"):
            continue

        dedupe_key = section.get("eventId") or (
            f"{category}:{section.get('magnitudeOfDelay')}:"
            f"{section.get('delayInSeconds')}:"
            f"{section.get('effectiveSpeedInKmh')}"
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        severity = incident_severity(section.get("magnitudeOfDelay"))
        delay_seconds = max(0, int(section.get("delayInSeconds") or 0))
        incidents.append(
            {
                "title": incident_title(category),
                "description": severity_description(severity),
                "severity": severity,
                "delay_minutes": round(delay_seconds / 60),
                "_delay_seconds": delay_seconds,
            }
        )

    incidents.sort(key=lambda item: item["_delay_seconds"], reverse=True)
    for incident in incidents:
        incident.pop("_delay_seconds", None)
    return incidents[:limit]


def build_traffic_payload(
    summary: dict,
    sections: list[dict],
    origin_name: str,
    destination_name: str,
    imperial: bool,
    max_incidents: int,
    now: datetime,
    map_url: str = "",
) -> dict:
    """Shape a TomTom route into the card and LLM payload."""
    travel_seconds = summary.get("travelTimeInSeconds")
    if travel_seconds is None:
        raise ValueError("TomTom returned a route without a travel time.")

    travel_seconds = max(0, int(travel_seconds))
    delay_seconds = max(0, int(summary.get("trafficDelayInSeconds") or 0))
    free_flow_seconds = summary.get("noTrafficTravelTimeInSeconds")
    if free_flow_seconds is None:
        free_flow_seconds = max(0, travel_seconds - delay_seconds)
    free_flow_seconds = max(0, int(free_flow_seconds))

    length_meters = float(summary.get("lengthInMeters") or 0)
    travel_minutes = max(1, round(travel_seconds / 60))
    delay_minutes = round(delay_seconds / 60)
    free_flow_minutes = max(1, round(free_flow_seconds / 60))
    traffic_level = classify_traffic(delay_seconds, free_flow_seconds)
    incidents = collect_incidents(sections, max_incidents)
    arrival = now + timedelta(seconds=travel_seconds)
    via = extract_via(sections)
    description = route_description(incidents, delay_minutes)

    if delay_minutes >= 1:
        delay_phrase = f"about {delay_minutes} min longer than free-flow traffic"
    else:
        delay_phrase = "with no significant delay"
    summary_parts = [
        f"{origin_name} to {destination_name} takes about "
        f"{travel_minutes} min, {delay_phrase}."
    ]
    if incidents:
        count = len(incidents)
        summary_parts.append(
            f"There {'is' if count == 1 else 'are'} {count} "
            f"reported incident{'s' if count != 1 else ''} on the route."
        )
    else:
        summary_parts.append("No incidents are reported on the route.")

    return {
        "source": "tomtom",
        "origin": origin_name,
        "destination": destination_name,
        "via": via,
        "description": description,
        "distance": format_distance(length_meters, imperial),
        "distance_long": format_distance_long(length_meters, imperial),
        "travel_time_minutes": travel_minutes,
        "free_flow_minutes": free_flow_minutes,
        "delay_minutes": delay_minutes,
        "traffic_level": traffic_level,
        "arrival_time": format_arrival_time(arrival),
        "map_url": map_url,
        "incidents": incidents,
        "summary": " ".join(summary_parts),
        "instruction": (
            "Answer with a short, natural summary of the drive. Mention "
            "the travel time, the delay, and any incidents. Do not read "
            "the raw data or list every field."
        ),
    }
