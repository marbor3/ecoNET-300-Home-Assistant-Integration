"""Tests for ecoVENT (Plum ventilation / heat-recovery controller) support.

Fixture data in ``fixtures/ecoVENT`` was recorded from a real ecoVENT unit
(ecoNET300 module S003.50); identifying sysParams values were removed.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, Mock

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature
import pytest

from custom_components.econet300.binary_sensor import create_binary_sensors
from custom_components.econet300.common_functions import (
    camel_to_snake,
    is_ecovent_controller,
)
from custom_components.econet300.const import (
    DOMAIN,
    ECOVENT_BINARY_SENSORS,
    ECOVENT_SENSORS,
    SENSOR_MAP_KEY,
    SERVICE_API,
    SERVICE_COORDINATOR,
)
from custom_components.econet300.ecovent import ECOVENT_WRITABLE_KEYS
from custom_components.econet300.sensor import (
    _controller_sensor_key_candidates,
    create_controller_sensors,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ecoVENT"
COMPONENT_DIR = Path(__file__).parent.parent / "custom_components" / "econet300"
TRANSLATION_FILES = [
    COMPONENT_DIR / "strings.json",
    *sorted((COMPONENT_DIR / "translations").glob("*.json")),
]


def _load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def coordinator() -> Mock:
    """Coordinator holding the recorded ecoVENT regParams/sysParams."""
    mock = Mock()
    mock.data = {
        "regParams": _load("regParams.json"),
        "sysParams": _load("sysParams.json"),
    }
    return mock


@pytest.fixture
def sensors_by_key(coordinator: Mock) -> dict:
    """Return controller sensors created for the ecoVENT fixture, keyed by API key."""
    return {
        e.entity_description.key: e.entity_description
        for e in create_controller_sensors(coordinator, Mock())
    }


class TestEcoventDetection:
    """controllerID detection."""

    @pytest.mark.parametrize("cid", ["ecoVENT", " ecoVENT ", "ecovent", "ecoVENT 300"])
    def test_matches_ecovent(self, cid: str) -> None:
        """All spellings of an ecoVENT controllerID are recognised."""
        assert is_ecovent_controller(cid)

    @pytest.mark.parametrize(
        "cid", [None, "", "ecoSOL", "ecoMAX850R2-X", "ecoMAX360i", 42]
    )
    def test_rejects_other_controllers(self, cid) -> None:
        """Boiler and solar controllers are not mistaken for ecoVENT."""
        assert not is_ecovent_controller(cid)

    def test_ecovent_uses_ecovent_sensor_set(self) -> None:
        """The ecoVENT key set replaces the boiler defaults, minus writable settings."""
        assert (
            _controller_sensor_key_candidates("ecoVENT")
            == ECOVENT_SENSORS - ECOVENT_WRITABLE_KEYS
        )
        assert SENSOR_MAP_KEY["ecoVENT"] is ECOVENT_SENSORS


class TestEcoventSensors:
    """Sensors created from the recorded ecoVENT regParams."""

    @pytest.mark.parametrize(
        "key",
        [
            "REKcurIntTemp",
            "REKcurSupTemp",
            "REKcurBehExchTemp",
            "REKcurExtTemp",
            "REKcuExhTemp",
        ],
    )
    def test_air_temperatures_are_celsius(self, sensors_by_key: dict, key: str) -> None:
        """Air temperatures get a unit, device class and are shown on the device page."""
        desc = sensors_by_key[key]
        assert desc.native_unit_of_measurement == UnitOfTemperature.CELSIUS
        assert desc.device_class == SensorDeviceClass.TEMPERATURE
        assert desc.entity_category is None

    @pytest.mark.parametrize(
        "key", ["REKcurSupFanSpeed", "REKcurExhFanSpeed", "BYPcurControl"]
    )
    def test_fans_and_bypass_are_percent(self, sensors_by_key: dict, key: str) -> None:
        """Fan speeds and bypass opening are percentages."""
        assert sensors_by_key[key].native_unit_of_measurement == PERCENTAGE

    def test_mode_presets_are_diagnostic(self, sensors_by_key: dict) -> None:
        """Read-only per-mode presets stay off the main device page."""
        desc = sensors_by_key["REKUser1SupFanSpeed"]
        assert desc.entity_category == EntityCategory.DIAGNOSTIC
        assert desc.native_unit_of_measurement == PERCENTAGE

    def test_writable_settings_are_not_duplicated_as_sensors(
        self, sensors_by_key: dict
    ) -> None:
        """Settings with a number/select entity get no read-only sensor copy."""
        assert not ECOVENT_WRITABLE_KEYS & set(sensors_by_key)

    def test_status_codes_have_no_statistics(self, sensors_by_key: dict) -> None:
        """Raw state codes are diagnostic and do not create long-term statistics."""
        desc = sensors_by_key["REKmodState"]
        assert desc.state_class is None
        assert desc.entity_category == EntityCategory.DIAGNOSTIC

    def test_null_values_are_not_created(self, sensors_by_key: dict) -> None:
        """A sensor whose value is null (no ground heat exchanger) is skipped."""
        assert "REKcurGHETemp" not in sensors_by_key

    def test_no_boiler_sensors(self, sensors_by_key: dict) -> None:
        """Boiler-only keys do not appear for a recuperator."""
        assert not {"tempCO", "boilerPower", "fuelLevel", "tempCWU"} & set(
            sensors_by_key
        )

    def test_every_recorded_key_has_a_name(self, sensors_by_key: dict) -> None:
        """Every ecoVENT regParams key is named in strings.json and all translations."""
        reg_keys = set(_load("regParams.json")) & ECOVENT_SENSORS
        assert reg_keys, "fixture keys should be covered by ECOVENT_SENSORS"
        for path in TRANSLATION_FILES:
            names = json.loads(path.read_text(encoding="utf-8"))["entity"]["sensor"]
            missing = sorted(k for k in reg_keys if camel_to_snake(k) not in names)
            assert not missing, f"{path.name} lacks names for {missing}"


class TestEcoventBoilerEntitiesSkipped:
    """Boiler-specific entities must not be created for a recuperator."""

    def test_binary_sensors_are_connectivity_only(self, coordinator: Mock) -> None:
        """Pumps, feeder and the fuel consumption calculator are not created."""
        keys = {
            e.entity_description.key for e in create_binary_sensors(coordinator, Mock())
        }
        assert keys <= ECOVENT_BINARY_SENSORS
        assert "fuelConsumptionCalc" not in keys
        assert "wifi" in keys

    @pytest.mark.asyncio
    async def test_no_boiler_switch(self, coordinator: Mock) -> None:
        """The boiler on/off switch (BOILER_CONTROL writes) is not created."""
        from custom_components.econet300 import switch  # noqa: PLC0415

        coordinator.data["mergedData"] = None
        hass = MagicMock()
        entry = MagicMock(entry_id="e1")
        hass.data = {
            DOMAIN: {"e1": {SERVICE_COORDINATOR: coordinator, SERVICE_API: Mock()}}
        }
        added: list = []
        await switch.async_setup_entry(hass, entry, added.extend)
        assert not any(e.entity_description.key == "boiler_control" for e in added)

    @pytest.mark.asyncio
    async def test_no_heater_mode_select(self, coordinator: Mock) -> None:
        """The boiler heater-mode select is not created."""
        from custom_components.econet300 import select  # noqa: PLC0415

        coordinator.data["mergedData"] = None
        hass = MagicMock()
        entry = MagicMock(entry_id="e1")
        hass.data = {
            DOMAIN: {"e1": {SERVICE_COORDINATOR: coordinator, SERVICE_API: Mock()}}
        }
        added: list = []
        await select.async_setup_entry(hass, entry, added.extend)
        assert not any(e.entity_description.key == "heater_mode" for e in added)
