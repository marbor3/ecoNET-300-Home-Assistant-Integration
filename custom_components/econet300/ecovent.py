"""User-level settings of ecoVENT ventilation (recuperator) controllers.

ecoVENT units report their user settings in ``editParams.data`` keyed by a
numeric id, with the controller's parameter name in ``name`` (for example
``{"286": {"name": "REKUser1SetPoint", "value": 18, "minv": 15, "maxv": 30}}``).
Some of them are also in ``regParams`` under the same name, which is polled far
more often, so values are read from ``regParams`` first.

Only the settings a user changes on the panel are exposed: the main operating
mode, the per-mode target temperature and airflow, and the party, airing and
away presets. Service parameters (PID constants, relay assignments, Modbus,
factory reset, ...) are deliberately not exposed. Writes go through
``newParam?newParamName=<name>``, like the ecoSTER thermostat setpoints.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import (
    EntityCategory,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolumeFlowRate,
)
from homeassistant.exceptions import HomeAssistantError

from .common import Econet300Api, EconetDataCoordinator
from .common_functions import camel_to_snake, is_ecovent_controller
from .const import DOMAIN
from .entity import EconetEntity

_LOGGER = logging.getLogger(__name__)

# Main operating mode (REKWS1). The controller lists its options in this order;
# values 3, 4, 5, 7 are user modes 1-4 (the cloud panel and the schedule editor
# call them "Tryb 1-4" / User1-User4), 6 runs the weekly schedule, 0 stops the unit.
ECOVENT_MODE_PARAM = "REKWS1"
ECOVENT_MODE_OPTIONS: dict[int, str] = {
    0: "stop",
    6: "schedule",
    3: "user_mode_1",
    4: "user_mode_2",
    5: "user_mode_3",
    7: "user_mode_4",
}

# Maximum airflow of the unit; caps presets whose own limit is larger (party and
# airing allow 4000 m3/h in editParams although the unit moves at most ENRECmaxFlow).
ECOVENT_MAX_FLOW_PARAM = "ENRECmaxFlow"


@dataclass(frozen=True)
class EcoventSetting:
    """One writable ecoVENT setting."""

    param: str
    kind: str  # "temp", "flow", "hours" or "minutes"


ECOVENT_NUMBER_SETTINGS: tuple[EcoventSetting, ...] = (
    *(EcoventSetting(f"REKUser{n}SetPoint", "temp") for n in (1, 2, 3, 4)),
    *(EcoventSetting(f"PRESALUser{n}SupFlow", "flow") for n in (1, 2, 3, 4)),
    *(EcoventSetting(f"PRESALUser{n}ExhFlow", "flow") for n in (1, 2, 3, 4)),
    EcoventSetting("REKPartySetPoint", "temp"),
    EcoventSetting("PRESALPartySupFlow", "flow"),
    EcoventSetting("PRESALPartyExhFlow", "flow"),
    EcoventSetting("REKPartyDur", "hours"),
    EcoventSetting("PRESALAiringExhFlow", "flow"),
    EcoventSetting("REKAiringDur", "minutes"),
    EcoventSetting("REKOutDur", "hours"),
)

# regParams keys that are now writable entities; the read-only all-sensors
# sweep must not create a second, read-only copy of them.
ECOVENT_WRITABLE_KEYS: frozenset[str] = frozenset(
    {s.param for s in ECOVENT_NUMBER_SETTINGS} | {ECOVENT_MODE_PARAM}
)

_KIND_ATTRS: dict[str, tuple[NumberDeviceClass, str]] = {
    "temp": (NumberDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
    "flow": (
        NumberDeviceClass.VOLUME_FLOW_RATE,
        UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR,
    ),
    "hours": (NumberDeviceClass.DURATION, UnitOfTime.HOURS),
    "minutes": (NumberDeviceClass.DURATION, UnitOfTime.MINUTES),
}


def ecovent_translation_key(param: str) -> str:
    """Return the translation key of an ecoVENT setting entity."""
    return f"ecovent_{camel_to_snake(param)}"


def find_edit_param(data: dict[str, Any] | None, name: str) -> tuple[str, dict] | None:
    """Return ``(id, entry)`` of the editParams entry called ``name``."""
    edit_params = (data or {}).get("editParams") or {}
    for pid, entry in edit_params.items():
        if isinstance(entry, dict) and entry.get("name") == name:
            return str(pid), entry
    return None


def ecovent_value(data: dict[str, Any] | None, name: str) -> Any:
    """Return the current value of setting ``name`` (regParams first, then editParams)."""
    reg_params = (data or {}).get("regParams") or {}
    if reg_params.get(name) is not None:
        return reg_params[name]
    found = find_edit_param(data, name)
    return found[1].get("value") if found else None


def _limits(
    data: dict[str, Any] | None, setting: EcoventSetting
) -> tuple[float, float] | None:
    """Return the controller's ``(min, max)`` for a setting, or None when unusable."""
    found = find_edit_param(data, setting.param)
    if found is None:
        return None
    entry = found[1]
    minv, maxv = entry.get("minv"), entry.get("maxv")
    if not entry.get("edit") or not all(
        isinstance(v, (int, float)) for v in (minv, maxv)
    ):
        return None
    if setting.kind == "flow":
        max_flow = ecovent_value(data, ECOVENT_MAX_FLOW_PARAM)
        if isinstance(max_flow, (int, float)) and max_flow > minv:
            maxv = min(maxv, max_flow)
    if maxv <= minv:
        return None
    return float(minv), float(maxv)


async def _write(entity: EconetEntity, name: str, value: int) -> None:
    """Write ``name=value`` and keep it until the controller confirms it."""
    if not await entity.api.set_param(name, value):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="number_set_failed",
            translation_placeholders={"error": f"{name}={value}"},
        )
    data = entity.coordinator.data or {}
    reg_params = data.get("regParams") or {}
    if name in reg_params:
        reg_params[name] = value
    found = find_edit_param(data, name)
    if found is not None:
        entity.coordinator.store_edit_param_write(found[0], value)


class EcoventNumber(EconetEntity, NumberEntity):
    """Writable ecoVENT setting (temperature, airflow or duration)."""

    entity_description: NumberEntityDescription

    def __init__(
        self,
        coordinator: EconetDataCoordinator,
        api: Econet300Api,
        description: NumberEntityDescription,
    ) -> None:
        """Initialize the setting number."""
        self.entity_description = description
        super().__init__(coordinator, api)

    def _lookup_value(self) -> Any:
        return ecovent_value(self.coordinator.data, self.entity_description.key)

    @property
    def available(self) -> bool:
        """Return True when data is fresh and the setting is reported."""
        return super().available and self._lookup_value() is not None

    def _sync_state(self, value: Any) -> None:
        self._attr_native_value = (
            float(value) if isinstance(value, (int, float)) else None
        )
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        """Write a new value, rejecting anything outside the controller's limits."""
        description = self.entity_description
        if not description.native_min_value <= value <= description.native_max_value:
            raise HomeAssistantError(
                f"{description.key}={value} outside "
                f"{description.native_min_value}-{description.native_max_value}"
            )
        send_value = round(value)
        await _write(self, description.key, send_value)
        self._attr_native_value = float(send_value)
        self.async_write_ha_state()


class EcoventModeSelect(EconetEntity, SelectEntity):
    """Main operating mode of an ecoVENT unit (stop, schedule, user modes 1-4)."""

    entity_description: SelectEntityDescription

    def __init__(
        self,
        coordinator: EconetDataCoordinator,
        api: Econet300Api,
        description: SelectEntityDescription,
    ) -> None:
        """Initialize the mode select."""
        self.entity_description = description
        super().__init__(coordinator, api)

    def _lookup_value(self) -> Any:
        return ecovent_value(self.coordinator.data, ECOVENT_MODE_PARAM)

    @property
    def available(self) -> bool:
        """Return True when data is fresh and the mode is a known option."""
        return super().available and self._lookup_value() in ECOVENT_MODE_OPTIONS

    def _sync_state(self, value: Any) -> None:
        self._attr_current_option = ECOVENT_MODE_OPTIONS.get(value)
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        """Switch the unit to another main mode."""
        value = next((v for v, o in ECOVENT_MODE_OPTIONS.items() if o == option), None)
        if value is None:
            raise HomeAssistantError(f"Unknown ecoVENT mode: {option}")
        await _write(self, ECOVENT_MODE_PARAM, value)
        self._attr_current_option = option
        self.async_write_ha_state()


def create_ecovent_numbers(
    coordinator: EconetDataCoordinator, api: Econet300Api
) -> list[EcoventNumber]:
    """Create setting numbers for an ecoVENT controller (empty for other controllers)."""
    data = coordinator.data or {}
    if not is_ecovent_controller((data.get("sysParams") or {}).get("controllerID")):
        return []
    entities: list[EcoventNumber] = []
    for setting in ECOVENT_NUMBER_SETTINGS:
        limits = _limits(data, setting)
        if limits is None:
            continue
        device_class, unit = _KIND_ATTRS[setting.kind]
        description = NumberEntityDescription(
            key=setting.param,
            translation_key=ecovent_translation_key(setting.param),
            device_class=device_class,
            native_unit_of_measurement=unit,
            native_min_value=limits[0],
            native_max_value=limits[1],
            native_step=1.0,
            mode=NumberMode.BOX,
            entity_category=EntityCategory.CONFIG,
        )
        entities.append(EcoventNumber(coordinator, api, description))
    _LOGGER.info("Created %d ecoVENT setting numbers", len(entities))
    return entities


def create_ecovent_selects(
    coordinator: EconetDataCoordinator, api: Econet300Api
) -> list[EcoventModeSelect]:
    """Create the main mode select for an ecoVENT controller."""
    data = coordinator.data or {}
    if not is_ecovent_controller((data.get("sysParams") or {}).get("controllerID")):
        return []
    if ecovent_value(data, ECOVENT_MODE_PARAM) not in ECOVENT_MODE_OPTIONS:
        _LOGGER.info("ecoVENT main mode not reported, mode select not created")
        return []
    description = SelectEntityDescription(
        key=ECOVENT_MODE_PARAM,
        translation_key="ecovent_main_mode",
        options=list(ECOVENT_MODE_OPTIONS.values()),
    )
    return [EcoventModeSelect(coordinator, api, description)]
