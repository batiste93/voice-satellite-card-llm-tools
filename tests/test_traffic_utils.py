"""Tests for the traffic tool's pure helpers."""

from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime
from pathlib import Path

MODULE_PATH = (
    Path(__file__).parent.parent
    / "custom_components"
    / "voice_satellite_llm_tools"
    / "traffic_utils.py"
)
_spec = importlib.util.spec_from_file_location("traffic_utils", MODULE_PATH)
traffic_utils = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(traffic_utils)

classify_traffic = traffic_utils.classify_traffic
format_arrival_time = traffic_utils.format_arrival_time
format_distance = traffic_utils.format_distance
incident_severity = traffic_utils.incident_severity
incident_title = traffic_utils.incident_title
match_saved_destination = traffic_utils.match_saved_destination
parse_saved_destinations = traffic_utils.parse_saved_destinations
severity_description = traffic_utils.severity_description
build_traffic_payload = traffic_utils.build_traffic_payload
collect_incidents = traffic_utils.collect_incidents
extract_via = traffic_utils.extract_via
format_distance_long = traffic_utils.format_distance_long
route_description = traffic_utils.route_description


class ParseSavedDestinationsTest(unittest.TestCase):
    def test_parses_equals_and_colon_lines(self):
        raw = """
        # commute
        Work = 100 Light St, Baltimore, MD
        Daycare: 200 Main St, Baltimore, MD
        BWI Airport

        """
        parsed = parse_saved_destinations(raw)

        self.assertEqual(
            parsed["work"], ("Work", "100 Light St, Baltimore, MD")
        )
        self.assertEqual(
            parsed["daycare"], ("Daycare", "200 Main St, Baltimore, MD")
        )
        self.assertEqual(parsed["bwi airport"], ("BWI Airport", "BWI Airport"))

    def test_matches_names_case_insensitively(self):
        raw = "Work = 100 Light St, Baltimore, MD"

        self.assertEqual(
            match_saved_destination(raw, "  WORK "),
            ("Work", "100 Light St, Baltimore, MD"),
        )
        self.assertIsNone(match_saved_destination(raw, "gym"))

    def test_empty_input_is_empty(self):
        self.assertEqual(parse_saved_destinations(""), {})
        self.assertIsNone(match_saved_destination("", "work"))


class TrafficLevelTest(unittest.TestCase):
    def test_short_delay_is_light(self):
        self.assertEqual(classify_traffic(60, 600), "light")

    def test_moderate_delay(self):
        # 6 min delay on a 30 min free-flow drive (20%).
        self.assertEqual(classify_traffic(360, 1800), "moderate")

    def test_heavy_delay(self):
        # 9 min delay on a 30 min drive (30%).
        self.assertEqual(classify_traffic(540, 1800), "heavy")

    def test_severe_delay(self):
        # 20 min delay on a 25 min drive (80%).
        self.assertEqual(classify_traffic(1200, 1500), "severe")

    def test_zero_free_flow_is_light(self):
        self.assertEqual(classify_traffic(0, 0), "light")


class FormattingTest(unittest.TestCase):
    def test_distance_imperial(self):
        self.assertEqual(format_distance(1609.344, True), "1.0 mi")

    def test_distance_metric(self):
        self.assertEqual(format_distance(1000, False), "1.0 km")

    def test_distance_invalid(self):
        self.assertEqual(format_distance(None, True), "")

    def test_arrival_time(self):
        self.assertEqual(
            format_arrival_time(datetime(2026, 10, 18, 16, 32)), "4:32 PM"
        )


class IncidentTest(unittest.TestCase):
    def test_titles(self):
        self.assertEqual(incident_title("JAM"), "Traffic jam")
        self.assertEqual(incident_title("ROAD_WORK"), "Road work")
        self.assertEqual(incident_title("ROAD_CLOSURE"), "Road closure")
        self.assertEqual(incident_title("OTHER"), "Incident")
        self.assertEqual(incident_title(None), "Incident")

    def test_severities(self):
        self.assertEqual(incident_severity(1), "minor")
        self.assertEqual(incident_severity(2), "moderate")
        self.assertEqual(incident_severity(3), "major")
        self.assertEqual(incident_severity(4), "closure")
        self.assertEqual(incident_severity(None), "minor")
        self.assertEqual(incident_severity("2"), "moderate")

    def test_descriptions(self):
        self.assertEqual(severity_description("major"), "Major delay")
        self.assertEqual(severity_description("closure"), "Long delay")
        self.assertEqual(severity_description("unknown"), "")


class BuildPayloadTest(unittest.TestCase):
    def setUp(self):
        self.summary = {
            "lengthInMeters": 22852,
            "travelTimeInSeconds": 2160,
            "trafficDelayInSeconds": 360,
            "noTrafficTravelTimeInSeconds": 1800,
        }
        self.sections = [
            {
                "sectionType": "IMPORTANT_ROAD_STRETCH",
                "importantRoadStretchIndex": 1,
                "roadNumbers": [{"text": "MD-2"}],
            },
            {
                "sectionType": "IMPORTANT_ROAD_STRETCH",
                "importantRoadStretchIndex": 0,
                "roadNumbers": [{"text": "US-50 W"}],
            },
            {
                "simpleCategory": "JAM",
                "magnitudeOfDelay": 3,
                "delayInSeconds": 180,
                "eventId": "jam-1",
            },
            {
                "simpleCategory": "ROAD_WORK",
                "magnitudeOfDelay": 1,
                "delayInSeconds": 120,
                "eventId": "work-1",
            },
        ]

    def test_builds_card_and_llm_fields(self):
        payload = build_traffic_payload(
            self.summary,
            self.sections,
            "Home",
            "Work",
            imperial=True,
            max_incidents=5,
            now=datetime(2026, 10, 18, 16, 0),
        )

        self.assertEqual(payload["origin"], "Home")
        self.assertEqual(payload["destination"], "Work")
        self.assertEqual(payload["travel_time_minutes"], 36)
        self.assertEqual(payload["free_flow_minutes"], 30)
        self.assertEqual(payload["delay_minutes"], 6)
        self.assertEqual(payload["traffic_level"], "moderate")
        self.assertEqual(payload["distance"], "14.2 mi")
        self.assertEqual(payload["distance_long"], "14.2 miles")
        self.assertEqual(payload["via"], "US-50 W")
        self.assertEqual(
            payload["description"],
            "Best route now, 2 incidents on the way.",
        )
        self.assertEqual(payload["arrival_time"], "4:36 PM")
        self.assertEqual(len(payload["incidents"]), 2)
        self.assertEqual(payload["incidents"][0]["title"], "Traffic jam")
        self.assertEqual(payload["incidents"][0]["severity"], "major")
        self.assertIn("6 min longer", payload["summary"])
        self.assertIn("2 reported incidents", payload["summary"])

    def test_missing_free_flow_is_derived(self):
        summary = dict(self.summary)
        del summary["noTrafficTravelTimeInSeconds"]

        payload = build_traffic_payload(
            summary,
            [],
            "Home",
            "Work",
            imperial=False,
            max_incidents=5,
            now=datetime(2026, 10, 18, 16, 0),
        )

        self.assertEqual(payload["free_flow_minutes"], 30)
        self.assertEqual(payload["distance"], "22.9 km")
        self.assertEqual(payload["incidents"], [])
        self.assertIn("No incidents", payload["summary"])

    def test_missing_travel_time_raises(self):
        with self.assertRaises(ValueError):
            build_traffic_payload(
                {},
                [],
                "Home",
                "Work",
                imperial=True,
                max_incidents=5,
                now=datetime(2026, 10, 18, 16, 0),
            )

    def test_incident_limit_and_dedupe(self):
        sections = [
            {
                "simpleCategory": "JAM",
                "magnitudeOfDelay": 2,
                "delayInSeconds": 300,
                "eventId": "same",
            },
            {
                "simpleCategory": "JAM",
                "magnitudeOfDelay": 2,
                "delayInSeconds": 300,
                "eventId": "same",
            },
            {
                "simpleCategory": "ROAD_CLOSURE",
                "magnitudeOfDelay": 4,
                "delayInSeconds": 60,
            },
            {
                "simpleCategory": "ROAD_WORK",
                "magnitudeOfDelay": 1,
                "delayInSeconds": 30,
            },
        ]

        incidents = collect_incidents(sections, limit=2)

        self.assertEqual(len(incidents), 2)
        self.assertEqual(incidents[0]["title"], "Traffic jam")
        self.assertEqual(incidents[1]["title"], "Road closure")
        self.assertEqual(incidents[1]["severity"], "closure")

    def test_zero_limit_hides_incidents(self):
        self.assertEqual(collect_incidents(self.sections, limit=0), [])


class DirectionsHelpersTest(unittest.TestCase):
    def test_extract_via_prefers_important_road_stretch_index(self):
        sections = [
            {
                "sectionType": "IMPORTANT_ROAD_STRETCH",
                "importantRoadStretchIndex": 2,
                "streetName": {"text": "Main St"},
            },
            {
                "sectionType": "IMPORTANT_ROAD_STRETCH",
                "importantRoadStretchIndex": 0,
                "roadNumbers": [{"text": "US-50 W"}],
            },
            {"sectionType": "TRAFFIC", "simpleCategory": "JAM"},
        ]
        self.assertEqual(extract_via(sections), "US-50 W")

    def test_extract_via_falls_back_to_street_name(self):
        sections = [
            {
                "sectionType": "IMPORTANT_ROAD_STRETCH",
                "importantRoadStretchIndex": 0,
                "streetName": {"text": "State Hwy 295"},
            }
        ]
        self.assertEqual(extract_via(sections), "State Hwy 295")
        self.assertEqual(extract_via([]), "")

    def test_distance_long(self):
        self.assertEqual(format_distance_long(49000, False), "49.0 kilometers")
        self.assertEqual(format_distance_long(1609.344, True), "1.0 mile")
        self.assertEqual(format_distance_long(49000, True), "30.4 miles")

    def test_route_description(self):
        self.assertEqual(
            route_description([], 0), "Best route now, light traffic."
        )
        self.assertEqual(
            route_description([], 4),
            "Best route now, about 4 min longer than usual.",
        )
        self.assertEqual(
            route_description([{"title": "Road work"}], 0),
            "Best route now, road work on the way.",
        )
        self.assertEqual(
            route_description([{"title": "Traffic jam"}, {"title": "Road work"}], 0),
            "Best route now, 2 incidents on the way.",
        )


if __name__ == "__main__":
    unittest.main()
