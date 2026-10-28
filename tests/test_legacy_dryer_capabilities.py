"""TP6X dryer reads from diagnostics without exposing unverified controls."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from custom_components.localthings.legacy_http import (
    table_for,
    to_resources,
    unmapped_wrappers,
)
from custom_components.localthings.registry.adapter import flatten
from custom_components.localthings.registry.by_type import for_device_by_model, resolve
from custom_components.localthings.registry.by_type.legacy_dryer import REGISTRY
from custom_components.localthings.registry.discovery import discover
from custom_components.localthings.registry.entities import BinarySensorDesc, SensorDesc

FIXTURES = Path(__file__).parent / "fixtures"


def _bodies():
    path = FIXTURES / "dryer_tp6x_dv80m8214aw_cooling_8888.json"
    return json.loads(path.read_text(encoding="utf-8"))["bodies"]


def _discover(bodies):
    resources = to_resources(bodies, table_for("Dryer"))
    registry = resolve(resources)
    assert registry is not None and registry.name == "dryer"
    unbound = []
    bound = discover(resources, registry.capabilities, log=unbound.append)
    assert unbound == []
    return bound, resources, flatten(bound, resources)


def test_real_cooling_capture_gets_readable_entities():
    """Issue #646 reports Cooling even though supportedProgress omits it."""
    bodies = _bodies()
    bound, _, state = _discover(bodies)
    assert state["machine_state"] == "active"
    assert state["cycle_active"] is True
    assert state["progress"] == "cooling"
    assert state["completion_minutes"] == 6
    assert state["progress_percentage"] == 82
    assert state["finish_time"] is not None
    assert state["remote_control"] is True
    assert state["legacy_dryer_cycle_code"] == "96"
    assert state["dry_level"] == "None"
    assert state["dry_time"] == "00:30:00"
    assert all(isinstance(entity.desc, (SensorDesc, BinarySensorDesc)) for entity in bound)


def test_canonical_fixture_matches_the_real_capture():
    from tests.conftest import _load_device

    assert to_resources(_bodies(), table_for("Dryer")) == _load_device("dryer_tp6x_8888")


@pytest.mark.parametrize("device_type", ["Washer", "Dryer"])
def test_transport_reads_the_capture_using_the_records_laundry_type(monkeypatch, device_type):
    from custom_components.localthings.legacy_http_transport import LegacyHttpTransport
    from custom_components.localthings.registry.batch import parse_device0_batch
    from tests.conftest import _load_device

    monkeypatch.setattr("custom_components.localthings.legacy_http_transport._STATE", {})
    transport = LegacyHttpTransport(
        "192.0.2.1", 8888, cert_pem="unused", key_pem="unused", token="unused", family="TP6X_DRYER"
    )
    requests = []

    def request(method, path, **kwargs):
        requests.append((method, path))
        if path == "/devices/0":
            return 200, {"Device": {"type": device_type, **_bodies()}}
        return 404, None

    monkeypatch.setattr(transport, "_request", request)
    code, batch = transport.read(["device", "0"], timeout=1)
    assert code == 0x45
    assert parse_device0_batch(batch) == _load_device("dryer_tp6x_8888")
    assert all(method == "GET" for method, _ in requests)


@pytest.mark.parametrize(
    ("machine", "phase", "active", "display_state", "display_phase"),
    [
        ("Ready", "None", False, "idle", "idle"),
        ("Run", "Drying", True, "active", "drying"),
        ("Run", "Cooling", True, "active", "cooling"),
        ("Pause", "Drying", False, "pause", "idle"),
        ("Run", "Finish", False, "active", "finish"),
    ],
)
def test_cycle_transitions(machine, phase, active, display_state, display_phase):
    bodies = deepcopy(_bodies())
    bodies["Operation"].update(
        state=machine, progress=phase, remainingTime="00:01:00", progressPercentage=97
    )
    bound, _, state = _discover(bodies)
    assert state["machine_state"] == display_state
    assert state["progress"] == display_phase
    assert state["cycle_active"] is active
    if phase == "Finish":
        assert state["completion_minutes"] == 0
        assert state["finish_time"] is None
        percentage = next(entity for entity in bound if entity.desc.key == "progress_percentage")
        rep = to_resources(bodies, table_for("Dryer"))[percentage.href]
        assert isinstance(percentage.desc, SensorDesc)
        assert percentage.desc.sticky_fn is not None
        assert percentage.desc.sticky_value_fn is not None
        assert percentage.desc.sticky_fn(rep)
        assert percentage.desc.sticky_value_fn(rep) == 100
    elif active:
        assert state["finish_time"] is not None
        assert state["progress_percentage"] == 97


def test_smart_control_off_is_reported():
    bodies = _bodies()
    bodies["Configuration"]["remoteControlEnabled"] = False
    assert _discover(bodies)[2]["remote_control"] is False


@pytest.mark.parametrize(
    ("wrapper", "field", "key"),
    [
        ("Operation", "power", "power_switch"),
        ("Washer", "wrinklePrevent", "wrinkle_prevent"),
    ],
)
@pytest.mark.parametrize(("value", "expected"), [("On", True), ("Off", False)])
def test_reported_flags_keep_their_polarity(wrapper, field, key, value, expected):
    bodies = _bodies()
    bodies[wrapper][field] = value
    assert _discover(bodies)[2][key] is expected


@pytest.mark.parametrize(
    ("wrapper", "field", "key"),
    [
        ("Operation", "power", "power_switch"),
        ("Washer", "wrinklePrevent", "wrinkle_prevent"),
    ],
)
@pytest.mark.parametrize("value", [None, "NotSupported", [], {}, 0])
def test_unrecognized_flags_stay_unknown(wrapper, field, key, value):
    bodies = _bodies()
    bodies[wrapper][field] = value
    assert _discover(bodies)[2].get(key) is None


@pytest.mark.parametrize(
    ("wrapper", "field", "key"),
    [
        ("Operation", "power", "power_switch"),
        ("Washer", "wrinklePrevent", "wrinkle_prevent"),
    ],
)
def test_missing_flags_stay_unknown(wrapper, field, key):
    bodies = _bodies()
    del bodies[wrapper][field]
    assert _discover(bodies)[2].get(key) is None


@pytest.mark.parametrize(
    ("value", "expected"), [("Enable", True), ("Disable", False), ("NotSupported", None)]
)
def test_damp_alert_distinguishes_off_from_unknown(value, expected):
    bodies = _bodies()
    bodies["Mode"]["options"] = ["Course_9A", f"MixedLoadBell_{value}"]
    assert _discover(bodies)[2]["damp_alert"] is expected


def test_energy_database_pointer_stays_a_visible_gap():
    assert unmapped_wrappers(_bodies(), table_for("Dryer")) == ["EnergyConsumption"]


def test_missing_optional_values_do_not_invent_damp_alert():
    bodies = _bodies()
    bodies["Mode"]["options"] = ["Course_9A"]
    assert "damp_alert" not in _discover(bodies)[2]


@pytest.mark.parametrize(
    ("model_num", "description", "legacy"),
    [
        ("TP6X_DV80M8214AW", "TP6X_DRYER", True),
        ("", "tp6x_dryer", True),
        ("DRYER", "", False),
        ("TP6X_DRYER", "UNKNOWN", False),
        ("TP6X_DRYER_DRUM", "TP6X_DRYER_DRUM", False),
        ("TP6X_DRYER_TOPLOAD", "TP6X_DRYER_TOPLOAD", False),
        ("", "TP6X_DRYER_EXTRA", False),
        ("", "TP6X_DRYER_DV80M8214AW", False),
    ],
)
def test_only_the_exact_description_routes_to_the_read_only_registry(
    model_num, description, legacy
):
    assert (for_device_by_model(model_num, description) is REGISTRY) is legacy


@pytest.mark.parametrize("description", ["TP6X_DRYER_DRUM", "TP6X_DRYER_TOPLOAD"])
def test_flexdry_description_rows_are_not_shadowed(monkeypatch, description):
    from custom_components.localthings.registry.by_type import _CONSUMER_DESCRIPTION_TO_KEY
    from custom_components.localthings.registry.by_type.dryer import REGISTRY as DRYER_REGISTRY

    monkeypatch.setitem(_CONSUMER_DESCRIPTION_TO_KEY, description, "dryer")
    assert for_device_by_model(description, description) is DRYER_REGISTRY


def test_registry_exposes_no_write_functions():
    assert all(
        isinstance(desc, (SensorDesc, BinarySensorDesc)) and getattr(desc, "write_fn", None) is None
        for capabilities in REGISTRY.capabilities.values()
        for capability in capabilities
        for desc in capability.entities
    )
