"""Tests for the writable ecoVENT settings (numbers and the main mode select).

``fixtures/ecoVENT/editParams.json`` is a subset of editParams recorded from a
real ecoVENT unit: the user settings, the unit's maximum airflow and one
service parameter that must not be exposed.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from homeassistant.components.number import NumberDeviceClass
from homeassistant.const import (
    EntityCategory,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolumeFlowRate,
)
from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.econet300.common import skip_edit_params
from custom_components.econet300.ecovent import (
    ECOVENT_MODE_OPTIONS,
    ECOVENT_TEMP_MODE_OPTIONS,
    ECOVENT_NUMBER_SETTINGS,
    create_ecovent_numbers,
    create_ecovent_selects,
    ecovent_translation_key,
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
    """Coordinator holding the recorded ecoVENT data, editParams included."""
    mock = Mock()
    mock.data = {
        "regParams": _load("regParams.json"),
        "sysParams": _load("sysParams.json"),
        "editParams": _load("editParams.json")["data"],
    }
    return mock


@pytest.fixture
def api() -> Mock:
    """Return an API whose writes succeed."""
    mock = Mock()
    mock.uid = "TEST-ECOVENT-UID"
    mock.set_param = AsyncMock(return_value=True)
    return mock


def _selects(coordinator: Mock, api: Mock) -> dict:
    return {
        e.entity_description.key: e for e in create_ecovent_selects(coordinator, api)
    }


def _numbers(coordinator: Mock, api: Mock) -> dict:
    entities = create_ecovent_numbers(coordinator, api)
    for entity in entities:
        entity.async_write_ha_state = Mock()
    return {e.entity_description.key: e for e in entities}


class TestEditParamsEnabled:
    """editParams is read for ecoVENT although no ecoSTER panel is connected."""

    def test_ecovent_does_not_skip_edit_params(self) -> None:
        """The settings live in editParams, so it must be fetched."""
        assert not skip_edit_params(_load("sysParams.json"))

    def test_boiler_without_ecoster_still_skips(self) -> None:
        """Other controllers keep the previous behaviour."""
        assert skip_edit_params({"controllerID": "ecoMAX850R2-X"})


class TestEcoventNumbers:
    """Setting numbers created from the recorded editParams."""

    def test_all_user_settings_created(self, coordinator: Mock, api: Mock) -> None:
        """Every configured user setting present on the unit gets a number."""
        assert set(_numbers(coordinator, api)) == {
            s.param for s in ECOVENT_NUMBER_SETTINGS
        }

    def test_service_parameters_are_not_exposed(
        self, coordinator: Mock, api: Mock
    ) -> None:
        """Service values such as the bypass PID gain are never writable."""
        numbers = _numbers(coordinator, api)
        assert "BYPPIDcontP" not in numbers
        assert "ENRECmaxFlow" not in numbers

    def test_mode_setpoint_uses_controller_limits(
        self, coordinator: Mock, api: Mock
    ) -> None:
        """A mode target temperature is a 15-30 degC config number."""
        entity = _numbers(coordinator, api)["REKUser1SetPoint"]
        desc = entity.entity_description
        assert (desc.native_min_value, desc.native_max_value) == (15.0, 30.0)
        assert desc.native_unit_of_measurement == UnitOfTemperature.CELSIUS
        assert desc.device_class == NumberDeviceClass.TEMPERATURE
        assert desc.entity_category == EntityCategory.CONFIG
        assert entity._lookup_value() == 18  # noqa: SLF001

    def test_airflow_in_cubic_meters(self, coordinator: Mock, api: Mock) -> None:
        """Mode airflows are read from editParams in m3/h."""
        entity = _numbers(coordinator, api)["PRESALUser1SupFlow"]
        desc = entity.entity_description
        assert desc.native_unit_of_measurement == (
            UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR
        )
        assert desc.native_max_value == 500.0
        assert entity._lookup_value() == 400  # noqa: SLF001

    def test_airflow_capped_at_unit_maximum(self, coordinator: Mock, api: Mock) -> None:
        """Party airflow allows 4000 in editParams but the unit moves at most 500."""
        desc = _numbers(coordinator, api)["PRESALPartySupFlow"].entity_description
        assert desc.native_max_value == 500.0

    def test_blank_limits_during_party_use_known_ranges(
        self, coordinator: Mock, api: Mock
    ) -> None:
        """While party runs a real unit reports minv=maxv=0 for party presets."""
        for pid in ("261", "937", "938", "934"):
            coordinator.data["editParams"][pid].update(minv=0, maxv=0)
        numbers = _numbers(coordinator, api)
        temp = numbers["REKPartySetPoint"].entity_description
        flow = numbers["PRESALPartySupFlow"].entity_description
        assert (temp.native_min_value, temp.native_max_value) == (15.0, 30.0)
        assert (flow.native_min_value, flow.native_max_value) == (0.0, 500.0)
        assert "PRESALAiringExhFlow" in numbers

    def test_durations(self, coordinator: Mock, api: Mock) -> None:
        """Party/away last hours, airing minutes."""
        numbers = _numbers(coordinator, api)
        assert (
            numbers["REKPartyDur"].entity_description.native_unit_of_measurement
            == UnitOfTime.HOURS
        )
        assert (
            numbers["REKAiringDur"].entity_description.native_unit_of_measurement
            == UnitOfTime.MINUTES
        )

    @pytest.mark.asyncio
    async def test_write_by_name(self, coordinator: Mock, api: Mock) -> None:
        """A new value is written by parameter name and kept until confirmed."""
        entity = _numbers(coordinator, api)["REKUser4SetPoint"]
        await entity.async_set_native_value(21.0)
        api.set_param.assert_awaited_once_with("REKUser4SetPoint", 21)
        coordinator.store_edit_param_write.assert_called_once_with("459", 21)
        assert entity.native_value == 21.0

    @pytest.mark.asyncio
    async def test_out_of_range_is_rejected(self, coordinator: Mock, api: Mock) -> None:
        """Values outside the controller's limits are never sent."""
        entity = _numbers(coordinator, api)["REKUser4SetPoint"]
        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(40.0)
        api.set_param.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_write_raises(self, coordinator: Mock, api: Mock) -> None:
        """A rejected write is reported and the value is not changed."""
        api.set_param = AsyncMock(return_value=False)
        entity = _numbers(coordinator, api)["REKUser4SetPoint"]
        with pytest.raises(HomeAssistantError):
            await entity.async_set_native_value(21.0)
        coordinator.store_edit_param_write.assert_not_called()

    def test_not_created_for_other_controllers(
        self, coordinator: Mock, api: Mock
    ) -> None:
        """Boiler controllers get no ecoVENT settings even with the same names."""
        coordinator.data["sysParams"] = {"controllerID": "ecoMAX850R2-X"}
        assert create_ecovent_numbers(coordinator, api) == []
        assert create_ecovent_selects(coordinator, api) == []


class TestEcoventModeSelect:
    """Main operating mode (REKWS1)."""

    @pytest.fixture
    def select(self, coordinator: Mock, api: Mock):
        """Return the main mode select with state writes stubbed."""
        entity = _selects(coordinator, api)["REKWS1"]
        entity.async_write_ha_state = Mock()
        return entity

    def test_current_mode(self, select) -> None:
        """REKWS1=4 is user mode 2 ("Tryb 2" on the panel)."""
        select._sync_state(select._lookup_value())  # noqa: SLF001
        assert select.current_option == "user_mode_2"
        assert select.options == list(ECOVENT_MODE_OPTIONS.values())

    @pytest.mark.asyncio
    async def test_select_writes_mode_value(
        self, select, api: Mock, coordinator: Mock
    ) -> None:
        """Choosing mode 1 writes REKWS1=3."""
        await select.async_select_option("user_mode_1")
        api.set_param.assert_awaited_once_with("REKWS1", 3)
        assert coordinator.data["regParams"]["REKWS1"] == 3
        assert select.current_option == "user_mode_1"

    @pytest.mark.asyncio
    async def test_unknown_option_is_rejected(self, select, api: Mock) -> None:
        """Only the known modes can be written."""
        with pytest.raises(HomeAssistantError):
            await select.async_select_option("turbo")
        api.set_param.assert_not_awaited()

    def test_unknown_mode_value_hides_select(
        self, coordinator: Mock, api: Mock
    ) -> None:
        """An unexpected REKWS1 value does not produce a select with a wrong label."""
        coordinator.data["regParams"]["REKWS1"] = 9
        coordinator.data["editParams"]["17"]["value"] = 9
        assert "REKWS1" not in _selects(coordinator, api)


class TestEcoventTemporaryModeSelect:
    """Temporary mode (REKWS4): off, airing, party, away."""

    def test_off_when_no_temporary_mode(self, coordinator: Mock, api: Mock) -> None:
        """REKWS4=0 (recorded fixture) shows "off"."""
        select = _selects(coordinator, api)["REKWS4"]
        select.async_write_ha_state = Mock()
        select._sync_state(select._lookup_value())  # noqa: SLF001
        assert select.current_option == "off"
        assert select.options == list(ECOVENT_TEMP_MODE_OPTIONS.values())

    def test_party_value_observed_on_real_unit(
        self, coordinator: Mock, api: Mock
    ) -> None:
        """Starting party on the panel of a real unit set REKWS4=2."""
        coordinator.data["regParams"]["REKWS4"] = 2
        select = _selects(coordinator, api)["REKWS4"]
        select.async_write_ha_state = Mock()
        select._sync_state(select._lookup_value())  # noqa: SLF001
        assert select.current_option == "party"

    @pytest.mark.asyncio
    async def test_turning_off_writes_zero(self, coordinator: Mock, api: Mock) -> None:
        """Choosing "off" ends the temporary mode with REKWS4=0."""
        coordinator.data["regParams"]["REKWS4"] = 2
        select = _selects(coordinator, api)["REKWS4"]
        select.async_write_ha_state = Mock()
        await select.async_select_option("off")
        api.set_param.assert_awaited_once_with("REKWS4", 0)
        coordinator.store_edit_param_write.assert_called_once_with("20", 0)


class TestEcoventControlNames:
    """Every control is named in strings.json and all translations."""

    def test_names_present(self) -> None:
        """Numbers and the select (with all its states) are translated."""
        number_keys = {
            ecovent_translation_key(s.param) for s in ECOVENT_NUMBER_SETTINGS
        }
        for path in TRANSLATION_FILES:
            entity = json.loads(path.read_text(encoding="utf-8"))["entity"]
            missing = sorted(number_keys - set(entity["number"]))
            assert not missing, f"{path.name} lacks number names {missing}"
            states = entity["select"]["ecovent_main_mode"]["state"]
            assert set(states) == set(ECOVENT_MODE_OPTIONS.values()), path.name
            states = entity["select"]["ecovent_temporary_mode"]["state"]
            assert set(states) == set(ECOVENT_TEMP_MODE_OPTIONS.values()), path.name
