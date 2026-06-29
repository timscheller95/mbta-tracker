"""
Unit tests for the MBTA Orange Line Tracker.

Tested:
  collect_vehicles  — MBTA response parsing, state updates, speed conversion
  collect_alerts    — lifecycle filtering, state updates
  Observable gauge callbacks — correct Observations from _state
  mbta_get          — HTTP error handling, error counter increments
"""

import pytest
import requests
from unittest.mock import MagicMock, patch

import main


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_vehicle(
    vid: str = "v1",
    direction_id: int = 1,
    status: str = "STOPPED_AT",
    occupancy: str | None = None,
    speed: float | None = None,
    lat: float | None = None,
    lng: float | None = None,
    label: str = "1234",
) -> dict:
    return {
        "id": vid,
        "attributes": {
            "direction_id": direction_id,
            "current_status": status,
            "occupancy_status": occupancy,
            "speed": speed,
            "latitude": lat,
            "longitude": lng,
            "label": label,
        },
    }


def make_alert(lifecycle: str = "ONGOING", aid: str = "a1") -> dict:
    return {"id": aid, "attributes": {"lifecycle": lifecycle}}


def mock_http_response(status_code: int = 200, json_body: dict | None = None, exc=None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.json.return_value = json_body or {"data": []}
    if exc:
        resp.raise_for_status.side_effect = exc
    else:
        resp.raise_for_status.return_value = None
    return resp


# ── collect_vehicles ──────────────────────────────────────────────────────────

class TestCollectVehicles:
    @patch("main.mbta_get")
    def test_returns_total_count(self, mock_get):
        mock_get.return_value = {"data": [make_vehicle("v1"), make_vehicle("v2")]}
        assert main.collect_vehicles() == 2

    @patch("main.mbta_get")
    def test_direction_counts_northbound_and_southbound(self, mock_get):
        mock_get.return_value = {"data": [
            make_vehicle("v1", direction_id=1),  # northbound
            make_vehicle("v2", direction_id=1),  # northbound
            make_vehicle("v3", direction_id=0),  # southbound
        ]}
        main.collect_vehicles()
        assert main._state.vehicle_count_by_direction == {"northbound": 2, "southbound": 1}

    @patch("main.mbta_get")
    def test_unknown_direction_id_maps_to_unknown(self, mock_get):
        mock_get.return_value = {"data": [make_vehicle(direction_id=99)]}
        main.collect_vehicles()
        assert main._state.vehicle_count_by_direction == {"unknown": 1}

    @patch("main.mbta_get")
    def test_status_counts(self, mock_get):
        mock_get.return_value = {"data": [
            make_vehicle("v1", status="STOPPED_AT"),
            make_vehicle("v2", status="IN_TRANSIT_TO"),
            make_vehicle("v3", status="IN_TRANSIT_TO"),
        ]}
        main.collect_vehicles()
        assert main._state.vehicle_count_by_status == {
            "STOPPED_AT": 1,
            "IN_TRANSIT_TO": 2,
        }

    @patch("main.mbta_get")
    def test_null_occupancy_defaults_to_no_data(self, mock_get):
        mock_get.return_value = {"data": [make_vehicle(occupancy=None)]}
        main.collect_vehicles()
        assert main._state.vehicle_count_by_occupancy.get("NO_DATA") == 1

    @patch("main.mbta_get")
    def test_explicit_occupancy_is_preserved(self, mock_get):
        mock_get.return_value = {"data": [make_vehicle(occupancy="MANY_SEATS_AVAILABLE")]}
        main.collect_vehicles()
        assert main._state.vehicle_count_by_occupancy == {"MANY_SEATS_AVAILABLE": 1}

    @patch("main.mbta_get")
    @patch("main._vehicle_speed")
    def test_speed_converted_from_mps_to_mph(self, mock_speed, mock_get):
        # 10 m/s × 2.23694 = 22.3694 mph
        mock_get.return_value = {"data": [make_vehicle(speed=10.0, direction_id=0)]}
        main.collect_vehicles()

        mock_speed.record.assert_called_once()
        recorded_value = mock_speed.record.call_args[0][0]
        assert abs(recorded_value - 22.3694) < 0.01

    @patch("main.mbta_get")
    @patch("main._vehicle_speed")
    def test_null_speed_is_not_recorded(self, mock_speed, mock_get):
        mock_get.return_value = {"data": [make_vehicle(speed=None)]}
        main.collect_vehicles()
        mock_speed.record.assert_not_called()

    @patch("main.mbta_get")
    @patch("main._vehicle_speed")
    def test_speed_label_includes_direction(self, mock_speed, mock_get):
        mock_get.return_value = {"data": [make_vehicle(speed=5.0, direction_id=1)]}
        main.collect_vehicles()
        attrs = mock_speed.record.call_args[0][1]
        assert attrs["direction"] == "northbound"
        assert attrs["route"] == "Orange"

    @patch("main.mbta_get")
    def test_empty_response_returns_zero_and_clears_state(self, mock_get):
        main._state.vehicle_count_by_direction = {"northbound": 5}
        mock_get.return_value = {"data": []}
        assert main.collect_vehicles() == 0
        assert main._state.vehicle_count_by_direction == {}

    @patch("main.mbta_get")
    def test_passes_correct_route_filter(self, mock_get):
        mock_get.return_value = {"data": []}
        main.collect_vehicles()
        mock_get.assert_called_once_with("/vehicles", {"filter[route]": "Orange"})


# ── collect_alerts ────────────────────────────────────────────────────────────

class TestCollectAlerts:
    @patch("main.mbta_get")
    def test_counts_all_active_lifecycles(self, mock_get):
        mock_get.return_value = {"data": [
            make_alert("NEW", "a1"),
            make_alert("ONGOING", "a2"),
            make_alert("ONGOING_UPCOMING", "a3"),
        ]}
        assert main.collect_alerts() == 3
        assert main._state.alert_count == 3

    @patch("main.mbta_get")
    def test_excludes_past_and_upcoming_alerts(self, mock_get):
        mock_get.return_value = {"data": [
            make_alert("PAST", "a1"),
            make_alert("UPCOMING", "a2"),
        ]}
        assert main.collect_alerts() == 0

    @patch("main.mbta_get")
    def test_mixed_lifecycles_only_counts_active(self, mock_get):
        mock_get.return_value = {"data": [
            make_alert("NEW", "a1"),
            make_alert("PAST", "a2"),
            make_alert("ONGOING", "a3"),
            make_alert("UPCOMING", "a4"),
            make_alert("ONGOING_UPCOMING", "a5"),
        ]}
        assert main.collect_alerts() == 3

    @patch("main.mbta_get")
    def test_missing_lifecycle_field_not_counted(self, mock_get):
        mock_get.return_value = {"data": [{"id": "a1", "attributes": {}}]}
        assert main.collect_alerts() == 0

    @patch("main.mbta_get")
    def test_empty_response(self, mock_get):
        mock_get.return_value = {"data": []}
        assert main.collect_alerts() == 0
        assert main._state.alert_count == 0

    @patch("main.mbta_get")
    def test_updates_state_alert_count(self, mock_get):
        main._state.alert_count = 99  # stale value from a prior poll
        mock_get.return_value = {"data": [make_alert("ONGOING")]}
        main.collect_alerts()
        assert main._state.alert_count == 1


# ── Observable gauge callbacks ────────────────────────────────────────────────

class TestObservableCallbacks:
    def test_direction_callback_yields_one_observation_per_direction(self):
        main._state.vehicle_count_by_direction = {"northbound": 8, "southbound": 7}
        obs = list(main._observe_vehicle_count_by_direction(None))
        assert len(obs) == 2
        by_dir = {o.attributes["direction"]: o.value for o in obs}
        assert by_dir == {"northbound": 8, "southbound": 7}

    def test_direction_callback_attaches_route_label(self):
        main._state.vehicle_count_by_direction = {"northbound": 1}
        obs = list(main._observe_vehicle_count_by_direction(None))
        assert obs[0].attributes["route"] == "Orange"

    def test_status_callback_yields_correct_values(self):
        main._state.vehicle_count_by_status = {"STOPPED_AT": 3, "IN_TRANSIT_TO": 12}
        obs = list(main._observe_vehicle_count_by_status(None))
        by_status = {o.attributes["status"]: o.value for o in obs}
        assert by_status == {"STOPPED_AT": 3, "IN_TRANSIT_TO": 12}

    def test_occupancy_callback_yields_correct_values(self):
        main._state.vehicle_count_by_occupancy = {"MANY_SEATS_AVAILABLE": 5, "NO_DATA": 2}
        obs = list(main._observe_vehicle_count_by_occupancy(None))
        by_occ = {o.attributes["occupancy_status"]: o.value for o in obs}
        assert by_occ == {"MANY_SEATS_AVAILABLE": 5, "NO_DATA": 2}

    def test_alert_count_callback_yields_single_observation(self):
        main._state.alert_count = 4
        obs = list(main._observe_alert_count(None))
        assert len(obs) == 1
        assert obs[0].value == 4
        assert obs[0].attributes == {"route": "Orange"}

    def test_direction_callback_is_empty_when_state_empty(self):
        assert list(main._observe_vehicle_count_by_direction(None)) == []

    def test_status_callback_is_empty_when_state_empty(self):
        assert list(main._observe_vehicle_count_by_status(None)) == []

    def test_alert_callback_yields_zero_when_no_alerts(self):
        main._state.alert_count = 0
        obs = list(main._observe_alert_count(None))
        assert obs[0].value == 0

    def test_lat_callback_yields_one_observation_per_vehicle(self):
        main._state.vehicle_positions = [
            {"vehicle_id": "y1", "label": "1850", "direction": "northbound", "status": "IN_TRANSIT_TO", "lat": 42.36, "lng": -71.06},
            {"vehicle_id": "y2", "label": "1851", "direction": "southbound", "status": "STOPPED_AT",   "lat": 42.34, "lng": -71.08},
        ]
        obs = list(main._observe_vehicle_latitude(None))
        assert len(obs) == 2
        values = {o.attributes["vehicle_id"]: o.value for o in obs}
        assert values == {"y1": 42.36, "y2": 42.34}

    def test_lng_callback_yields_correct_values(self):
        main._state.vehicle_positions = [
            {"vehicle_id": "y1", "label": "1850", "direction": "northbound", "status": "IN_TRANSIT_TO", "lat": 42.36, "lng": -71.06},
        ]
        obs = list(main._observe_vehicle_longitude(None))
        assert obs[0].value == -71.06
        assert obs[0].attributes["vehicle_id"] == "y1"
        assert obs[0].attributes["route"] == "Orange"

    def test_position_callbacks_empty_when_no_vehicles(self):
        assert list(main._observe_vehicle_latitude(None)) == []
        assert list(main._observe_vehicle_longitude(None)) == []


class TestCollectVehiclesPositions:
    @patch("main.mbta_get")
    def test_populates_vehicle_positions(self, mock_get):
        mock_get.return_value = {"data": [
            make_vehicle("y1", direction_id=1, status="IN_TRANSIT_TO", lat=42.36, lng=-71.06, label="1850"),
        ]}
        main.collect_vehicles()
        assert len(main._state.vehicle_positions) == 1
        pos = main._state.vehicle_positions[0]
        assert pos["vehicle_id"] == "y1"
        assert pos["label"] == "1850"
        assert pos["lat"] == 42.36
        assert pos["lng"] == -71.06
        assert pos["direction"] == "northbound"

    @patch("main.mbta_get")
    def test_vehicles_without_position_are_excluded(self, mock_get):
        mock_get.return_value = {"data": [
            make_vehicle("y1", lat=None, lng=None),
            make_vehicle("y2", lat=42.36, lng=-71.06),
        ]}
        main.collect_vehicles()
        assert len(main._state.vehicle_positions) == 1
        assert main._state.vehicle_positions[0]["vehicle_id"] == "y2"

    @patch("main.mbta_get")
    def test_empty_response_clears_positions(self, mock_get):
        main._state.vehicle_positions = [{"vehicle_id": "y1", "lat": 42.0, "lng": -71.0}]
        mock_get.return_value = {"data": []}
        main.collect_vehicles()
        assert main._state.vehicle_positions == []


# ── collect_alerts (rider-focused) ────────────────────────────────────────────

def make_rich_alert(
    lifecycle: str = "ONGOING",
    aid: str = "a1",
    effect: str = "DELAY",
    direction_ids: list[int | None] | None = None,
    stop_ids: list[str | None] | None = None,
) -> dict:
    """Build an alert payload with informed_entity entries."""
    entities = []
    direction_ids = direction_ids or []
    stop_ids = stop_ids or [None] * len(direction_ids)
    for did, sid in zip(direction_ids, stop_ids):
        entity: dict = {}
        if did is not None:
            entity["direction_id"] = did
        if sid is not None:
            entity["stop"] = sid
        entities.append(entity)
    return {
        "id": aid,
        "attributes": {
            "lifecycle": lifecycle,
            "effect": effect,
            "informed_entity": entities,
        },
    }


class TestCollectAlertsRiderFocused:
    @patch("main.mbta_get")
    def test_effect_counted_by_direction(self, mock_get):
        mock_get.return_value = {"data": [
            make_rich_alert("ONGOING", "a1", "DELAY", direction_ids=[1]),
            make_rich_alert("ONGOING", "a2", "DELAY", direction_ids=[1]),
            make_rich_alert("ONGOING", "a3", "DELAY", direction_ids=[0]),
        ]}
        main.collect_alerts()
        assert main._state.alert_count_by_effect[("DELAY", "northbound")] == 2
        assert main._state.alert_count_by_effect[("DELAY", "southbound")] == 1

    @patch("main.mbta_get")
    def test_mixed_directions_map_to_both(self, mock_get):
        mock_get.return_value = {"data": [
            make_rich_alert("ONGOING", "a1", "SUSPENSION", direction_ids=[0, 1]),
        ]}
        main.collect_alerts()
        assert ("SUSPENSION", "both") in main._state.alert_count_by_effect

    @patch("main.mbta_get")
    def test_no_direction_ids_map_to_both(self, mock_get):
        mock_get.return_value = {"data": [
            make_rich_alert("ONGOING", "a1", "STOP_CLOSURE", direction_ids=[]),
        ]}
        main.collect_alerts()
        assert ("STOP_CLOSURE", "both") in main._state.alert_count_by_effect

    @patch("main.mbta_get")
    def test_stop_recorded_for_known_orange_line_stop(self, mock_get):
        mock_get.return_value = {"data": [
            make_rich_alert(
                "ONGOING", "a1", "DELAY",
                direction_ids=[1],
                stop_ids=["place-dwnxg"],
            ),
        ]}
        main.collect_alerts()
        key = ("place-dwnxg", "Downtown Crossing", "DELAY", "northbound")
        assert main._state.affected_stops[key] == 1

    @patch("main.mbta_get")
    def test_unknown_stop_not_recorded(self, mock_get):
        mock_get.return_value = {"data": [
            make_rich_alert(
                "ONGOING", "a1", "DELAY",
                direction_ids=[1],
                stop_ids=["place-unknown-xyz"],
            ),
        ]}
        main.collect_alerts()
        assert main._state.affected_stops == {}

    @patch("main.mbta_get")
    def test_past_alerts_excluded(self, mock_get):
        mock_get.return_value = {"data": [
            make_rich_alert("PAST", "a1", "DELAY", direction_ids=[1]),
        ]}
        main.collect_alerts()
        assert main._state.alert_count == 0
        assert main._state.alert_count_by_effect == {}

    @patch("main.mbta_get")
    def test_clears_stale_state(self, mock_get):
        main._state.alert_count_by_effect = {("DELAY", "northbound"): 5}
        main._state.affected_stops = {("place-dwnxg", "Downtown Crossing", "DELAY", "northbound"): 1}
        mock_get.return_value = {"data": []}
        main.collect_alerts()
        assert main._state.alert_count_by_effect == {}
        assert main._state.affected_stops == {}


# ── collect_predictions ────────────────────────────────────────────────────────

def make_prediction(direction_id: int, arrival_time: str | None, aid: str = "p1") -> dict:
    return {
        "id": aid,
        "attributes": {
            "direction_id": direction_id,
            "arrival_time": arrival_time,
            "departure_time": None,
        },
    }


class TestCollectPredictions:
    @patch("main.mbta_get")
    def test_computes_headway_for_direction(self, mock_get, monkeypatch):
        # Three northbound trains arriving 5 minutes apart → 300s headway
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        times = [
            (now + timedelta(minutes=5)).isoformat(),
            (now + timedelta(minutes=10)).isoformat(),
            (now + timedelta(minutes=15)).isoformat(),
        ]
        mock_get.return_value = {"data": [
            make_prediction(1, times[0], "p1"),
            make_prediction(1, times[1], "p2"),
            make_prediction(1, times[2], "p3"),
        ]}
        main.collect_predictions()
        assert abs(main._state.headway_seconds.get("northbound", 0) - 300) < 1

    @patch("main.mbta_get")
    def test_ignores_past_arrivals(self, mock_get):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        past = (now - timedelta(minutes=5)).isoformat()
        future1 = (now + timedelta(minutes=5)).isoformat()
        future2 = (now + timedelta(minutes=10)).isoformat()
        mock_get.return_value = {"data": [
            make_prediction(0, past,    "p1"),  # in the past — ignored
            make_prediction(0, future1, "p2"),
            make_prediction(0, future2, "p3"),
        ]}
        main.collect_predictions()
        # Only 2 future arrivals → 1 gap = 300s
        assert abs(main._state.headway_seconds.get("southbound", 0) - 300) < 1

    @patch("main.mbta_get")
    def test_single_prediction_produces_no_headway(self, mock_get):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        mock_get.return_value = {"data": [
            make_prediction(1, (now + timedelta(minutes=5)).isoformat()),
        ]}
        main.collect_predictions()
        assert "northbound" not in main._state.headway_seconds

    @patch("main.mbta_get")
    def test_empty_response_clears_headway(self, mock_get):
        main._state.headway_seconds = {"northbound": 300.0}
        mock_get.return_value = {"data": []}
        main.collect_predictions()
        assert main._state.headway_seconds == {}

    @patch("main.mbta_get")
    def test_queries_correct_stop_and_route(self, mock_get):
        mock_get.return_value = {"data": []}
        main.collect_predictions()
        _, kwargs_or_args = mock_get.call_args
        call_params = mock_get.call_args[0][1] if len(mock_get.call_args[0]) > 1 else mock_get.call_args[1].get("params", {})
        # Just verify it was called with the predictions endpoint and reference stop
        args = mock_get.call_args[0]
        assert args[0] == "/predictions"
        assert args[1]["filter[stop]"] == "place-dwnxg"
        assert args[1]["filter[route]"] == "Orange"

    @patch("main.mbta_get")
    def test_uses_departure_time_when_arrival_time_absent(self, mock_get):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        mock_get.return_value = {"data": [
            {
                "id": "p1",
                "attributes": {
                    "direction_id": 1,
                    "arrival_time": None,
                    "departure_time": (now + timedelta(minutes=5)).isoformat(),
                },
            },
            {
                "id": "p2",
                "attributes": {
                    "direction_id": 1,
                    "arrival_time": None,
                    "departure_time": (now + timedelta(minutes=11)).isoformat(),
                },
            },
        ]}
        main.collect_predictions()
        assert abs(main._state.headway_seconds.get("northbound", 0) - 360) < 1


# ── Observable gauge callbacks (rider-focused) ────────────────────────────────

class TestRiderObservableCallbacks:
    def test_alert_by_effect_yields_per_bucket(self):
        main._state.alert_count_by_effect = {
            ("DELAY", "northbound"): 2,
            ("STOP_CLOSURE", "both"): 1,
        }
        obs = list(main._observe_alert_by_effect(None))
        assert len(obs) == 2
        by_key = {(o.attributes["effect"], o.attributes["direction"]): o.value for o in obs}
        assert by_key == {("DELAY", "northbound"): 2, ("STOP_CLOSURE", "both"): 1}

    def test_alert_by_effect_attaches_route(self):
        main._state.alert_count_by_effect = {("DELAY", "southbound"): 1}
        obs = list(main._observe_alert_by_effect(None))
        assert obs[0].attributes["route"] == "Orange"

    def test_alert_by_effect_empty_when_no_alerts(self):
        assert list(main._observe_alert_by_effect(None)) == []

    def test_stop_alert_yields_one_observation_per_stop(self):
        main._state.affected_stops = {
            ("place-dwnxg", "Downtown Crossing", "DELAY", "northbound"): 1,
            ("place-bbsta", "Back Bay",          "DELAY", "southbound"): 1,
        }
        obs = list(main._observe_stop_alert(None))
        assert len(obs) == 2
        stop_names = {o.attributes["stop_name"] for o in obs}
        assert stop_names == {"Downtown Crossing", "Back Bay"}

    def test_stop_alert_observation_value_is_one(self):
        main._state.affected_stops = {
            ("place-dwnxg", "Downtown Crossing", "DELAY", "northbound"): 1,
        }
        obs = list(main._observe_stop_alert(None))
        assert obs[0].value == 1

    def test_stop_alert_empty_when_no_stops(self):
        assert list(main._observe_stop_alert(None)) == []

    def test_headway_yields_per_direction(self):
        main._state.headway_seconds = {"northbound": 300.0, "southbound": 420.0}
        obs = list(main._observe_headway(None))
        assert len(obs) == 2
        by_dir = {o.attributes["direction"]: o.value for o in obs}
        assert by_dir == {"northbound": 300.0, "southbound": 420.0}

    def test_headway_attaches_route_and_stop(self):
        main._state.headway_seconds = {"northbound": 300.0}
        obs = list(main._observe_headway(None))
        assert obs[0].attributes["route"] == "Orange"
        assert obs[0].attributes["reference_stop"] == "Downtown Crossing"

    def test_headway_empty_when_no_predictions(self):
        assert list(main._observe_headway(None)) == []

    def test_alert_info_yields_one_observation_per_alert(self):
        main._state.active_alerts = [
            {"alert_id": "a1", "effect": "DELAY", "direction": "northbound", "header": "Delays on Orange Line"},
            {"alert_id": "a2", "effect": "STATION_ISSUE", "direction": "both", "header": "Elevator outage at Back Bay"},
        ]
        obs = list(main._observe_alert_info(None))
        assert len(obs) == 2

    def test_alert_info_value_is_always_one(self):
        main._state.active_alerts = [
            {"alert_id": "a1", "effect": "DELAY", "direction": "northbound", "header": "Some delay"},
        ]
        obs = list(main._observe_alert_info(None))
        assert obs[0].value == 1

    def test_alert_info_carries_header_label(self):
        main._state.active_alerts = [
            {"alert_id": "a1", "effect": "DELAY", "direction": "northbound", "header": "Delays due to police activity"},
        ]
        obs = list(main._observe_alert_info(None))
        assert obs[0].attributes["header"] == "Delays due to police activity"
        assert obs[0].attributes["alert_id"] == "a1"
        assert obs[0].attributes["route"] == "Orange"

    def test_alert_info_empty_when_no_active_alerts(self):
        assert list(main._observe_alert_info(None)) == []


class TestCollectAlertsInfoMetric:
    @patch("main.mbta_get")
    def test_populates_active_alerts_with_header(self, mock_get):
        mock_get.return_value = {"data": [
            {
                "id": "a1",
                "attributes": {
                    "lifecycle": "ONGOING",
                    "effect": "DELAY",
                    "header": "Delays due to an earlier incident",
                    "informed_entity": [{"direction_id": 1}],
                },
            }
        ]}
        main.collect_alerts()
        assert len(main._state.active_alerts) == 1
        assert main._state.active_alerts[0]["header"] == "Delays due to an earlier incident"
        assert main._state.active_alerts[0]["alert_id"] == "a1"

    @patch("main.mbta_get")
    def test_clears_active_alerts_on_empty_response(self, mock_get):
        main._state.active_alerts = [{"alert_id": "a1", "effect": "DELAY", "direction": "northbound", "header": "old"}]
        mock_get.return_value = {"data": []}
        main.collect_alerts()
        assert main._state.active_alerts == []

    @patch("main.mbta_get")
    def test_null_header_stored_as_empty_string(self, mock_get):
        mock_get.return_value = {"data": [
            {
                "id": "a1",
                "attributes": {
                    "lifecycle": "ONGOING",
                    "effect": "STATION_ISSUE",
                    "header": None,
                    "informed_entity": [],
                },
            }
        ]}
        main.collect_alerts()
        assert main._state.active_alerts[0]["header"] == ""


# ── mbta_get ──────────────────────────────────────────────────────────────────

class TestMbtaGet:
    def test_returns_parsed_json_body(self):
        body = {"data": [{"id": "v1"}]}
        with patch.object(main._http, "get", return_value=mock_http_response(json_body=body)):
            result = main.mbta_get("/vehicles", {"filter[route]": "Orange"})
        assert result == body

    def test_raises_http_error_on_4xx(self):
        exc = requests.HTTPError("404 Not Found")
        with patch.object(main._http, "get", return_value=mock_http_response(404, exc=exc)):
            with pytest.raises(requests.HTTPError):
                main.mbta_get("/vehicles")

    def test_raises_http_error_on_5xx(self):
        exc = requests.HTTPError("503 Service Unavailable")
        with patch.object(main._http, "get", return_value=mock_http_response(503, exc=exc)):
            with pytest.raises(requests.HTTPError):
                main.mbta_get("/vehicles")

    def test_raises_on_connection_error(self):
        with patch.object(main._http, "get", side_effect=requests.ConnectionError("refused")):
            with pytest.raises(requests.ConnectionError):
                main.mbta_get("/vehicles")

    def test_raises_on_timeout(self):
        with patch.object(main._http, "get", side_effect=requests.Timeout("timed out")):
            with pytest.raises(requests.Timeout):
                main.mbta_get("/vehicles")

    @patch("main._api_errors")
    def test_increments_error_counter_on_http_error(self, mock_errors):
        exc = requests.HTTPError("500")
        with patch.object(main._http, "get", return_value=mock_http_response(500, exc=exc)):
            with pytest.raises(requests.HTTPError):
                main.mbta_get("/vehicles")
        mock_errors.add.assert_called_once()
        count, attrs = mock_errors.add.call_args[0]
        assert count == 1
        assert attrs["error"] == "http_error"

    @patch("main._api_errors")
    def test_increments_error_counter_on_connection_error(self, mock_errors):
        with patch.object(main._http, "get", side_effect=requests.ConnectionError()):
            with pytest.raises(requests.ConnectionError):
                main.mbta_get("/vehicles")
        mock_errors.add.assert_called_once()
        _, attrs = mock_errors.add.call_args[0]
        assert attrs["error"] == "ConnectionError"

    @patch("main._api_requests")
    def test_increments_request_counter_on_success(self, mock_requests):
        with patch.object(main._http, "get", return_value=mock_http_response(200)):
            main.mbta_get("/vehicles")
        mock_requests.add.assert_called_once()
        count, attrs = mock_requests.add.call_args[0]
        assert count == 1
        assert attrs["http.status_code"] == "200"

    @patch("main._api_duration")
    def test_records_duration_on_success(self, mock_duration):
        with patch.object(main._http, "get", return_value=mock_http_response(200)):
            main.mbta_get("/alerts")
        mock_duration.record.assert_called_once()
        duration, attrs = mock_duration.record.call_args[0]
        assert duration >= 0
        assert attrs["endpoint"] == "alerts"
