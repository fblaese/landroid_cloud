"""Define device classes."""
from __future__ import annotations
from functools import partial
import json

import logging
from typing import Any

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)

from homeassistant.components.select import (
    SelectEntity,
    SelectEntityDescription,
)

from homeassistant.components.vacuum import (
    ENTITY_ID_FORMAT,
    STATE_DOCKED,
    STATE_ERROR,
    STATE_RETURNING,
    StateVacuumEntity,
    VacuumEntityFeature,
)

from homeassistant.const import CONF_TYPE
from homeassistant.core import callback, HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory

from pyworxcloud import WorxCloud
from pyworxcloud.states import ERROR_TO_DESCRIPTION

from . import LandroidAPI
from .utils import pass_thru, parseday

from .attribute_map import ATTR_MAP

from .const import (
    ATTR_ZONE,
    DOMAIN,
    SCHEDULE_TO_DAY,
    SCHEDULE_TYPE_MAP,
    SERVICE_SETZONE,
    STATE_INITIALIZING,
    STATE_MAP,
    STATE_MOWING,
    STATE_OFFLINE,
    STATE_RAINDELAY,
    UPDATE_SIGNAL,
    UPDATE_SIGNAL_ZONES,
)

from .helpers import LandroidButtonTypes, LandroidSelectTypes

# Commonly supported features
SUPPORT_LANDROID_BASE = (
    VacuumEntityFeature.BATTERY
    | VacuumEntityFeature.PAUSE
    | VacuumEntityFeature.RETURN_HOME
    | VacuumEntityFeature.START
    | VacuumEntityFeature.STATE
    | VacuumEntityFeature.STATUS
)

# Tuple containing buttons to create
BUTTONS = [
    ButtonEntityDescription(
        key=LandroidButtonTypes.RESTART,
        name="Restart",
        icon="mdi:restart",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=ButtonDeviceClass.RESTART,
    ),
    ButtonEntityDescription(
        key=LandroidButtonTypes.EDGECUT,
        name="Start cutting edge",
        icon="mdi:map-marker-path",
        entity_category=None,
    ),
]

# Tuple containing select entities to create
SELECT = [
    SelectEntityDescription(
        key=LandroidSelectTypes.NEXT_ZONE,
        name="Select Next Zone",
        icon="mdi:map-clock",
        entity_category=EntityCategory.CONFIG,
    ),
]

_LOGGER = logging.getLogger(__name__)


class LandroidCloudBaseEntity:
    """Define a base Landroid class."""

    _battery_level: int | None = None
    _attr_state = STATE_INITIALIZING

    def __init__(self, hass, api):
        """Init new base device."""
        _LOGGER.debug("Initializing LandroidEntity for %s", api.name)
        self.api = api
        self.hass = hass
        self.entity_id = ENTITY_ID_FORMAT.format(f"{api.name}")

        self._attributes = {}
        self._available = False
        self._unique_id = f"{api.device.serial_number}_{api.name}"
        self._serialnumber = None
        self._icon = None
        self._name = f"{api.friendly_name}"
        self._mac = api.device.mac
        self._connections = {(dr.CONNECTION_NETWORK_MAC, self._mac)}

    @property
    def device_info(self):
        """Return device info"""
        return {
            "connections": self._connections,
            "identifiers": {
                (DOMAIN, self.api.unique_id, self.api.entry_id, self.api.friendly_name)
            },
            "name": str(self._name),
            "sw_version": self.api.device.firmware_version,
            "manufacturer": self.api.data.get(CONF_TYPE),
            "model": self.api.device.board,
        }

    async def async_added_to_hass(self):
        """Connect update callbacks."""
        _LOGGER.debug("Added sensor %s", self.entity_id)
        await self.api.async_refresh()
        async_dispatcher_connect(
            self.hass,
            f"{UPDATE_SIGNAL}_{self.api.device.name}",
            self.update_callback,
        )
        async_dispatcher_connect(
            self.hass,
            f"{UPDATE_SIGNAL_ZONES}_{self.api.device.name}",
            self.update_selected_zone,
        )

    @callback
    def update_callback(self):
        """Base update callback function"""
        return False

    @callback
    def update_selected_zone(self):
        """Update zone selections in select entity"""
        return False

    def zone_mapping(self):
        """Map zones correct."""
        return False

    async def async_update(self):
        """Update the device."""
        _LOGGER.debug("Updating %s", self.entity_id)
        master: WorxCloud = self.api.device

        methods = ATTR_MAP["default"]
        data = {}
        self._icon = methods["icon"]
        for prop, attr in methods["state"].items():
            if hasattr(master, prop):
                prop_data = getattr(master, prop)
                if not isinstance(prop_data, type(None)):
                    data[attr] = prop_data
        data["error"] = ERROR_TO_DESCRIPTION[master.error or 0]

        self._attributes.update(data)

        _LOGGER.debug("Mower %s online: %s", self._name, master.online)
        self._available = master.online
        state = STATE_INITIALIZING
        
        if not master.online:
            state = STATE_OFFLINE
        elif master.error is not None and master.error > 0:
            if master.error > 0 and master.error != 5:
                state = STATE_ERROR
            elif master.error == 5:
                state = STATE_RAINDELAY
        else:
            try:
                state = STATE_MAP[master.status]
            except KeyError:
                state = STATE_INITIALIZING

        if "zone_probability" in self._attributes:
            if len(self._attributes["zone_probability"]) == 10:
                self.zone_mapping()

        _LOGGER.debug("\nAttributes:\n%s", self._attributes)
        _LOGGER.debug("Mower %s state '%s'", self._name, state)
        # self._state = state
        self._attr_state = state

        self._serialnumber = master.serial
        self._battery_level = master.battery_percent


class LandroidCloudSelectEntity(LandroidCloudBaseEntity, SelectEntity):
    """Define a select entity."""

    def __init__(
        self,
        description: SelectEntityDescription,
        hass: HomeAssistant,
        api: LandroidAPI,
    ):
        """Initialize select entity."""
        super().__init__(hass, api)
        _LOGGER.debug("Initializing LandroidCloudSelectEntity for %s", api.name)
        self.entity_description = description
        self.entity_description.name = (
            f"{api.friendly_name} {description.key.capitalize()}"
        )
        self._attr_unique_id = f"{api.name}_select_{description.key}"
        self._attr_options = []
        self._attr_current_option = None
        self.entity_id = ENTITY_ID_FORMAT.format(self.entity_description.name)


class LandroidCloudSelectZoneEntity(LandroidCloudSelectEntity):
    """Select zone entity definition."""

    @callback
    def update_selected_zone(self):
        """Get new data and update state."""
        if not isinstance(self._attr_options, type(None)):
            if len(self._attr_options) > 1:
                self._update_zone()

            try:
                self._attr_current_option = str(self.api.shared_options["current_zone"])
            except:  # pylint: disable=bare-except
                self._attr_current_option = None
            finally:
                _LOGGER.debug(
                    "Zone selector for %s was set to %s",
                    self._name,
                    self._attr_current_option,
                )

        self.schedule_update_ha_state(True)

    def _update_zone(self) -> None:
        """Update zone selector options."""
        try:
            zones = self.api.device.zone
        except:  # pylint: disable=bare-except
            zones = []

        if len(zones) == 4:
            _LOGGER.debug("Updating select entity for %s", self._name)
            options = []
            options.append("0")
            for idx in range(1, 4):
                if zones[idx] != 0:
                    options.append(str(idx))

            self._attr_options = options
            _LOGGER.debug(
                "Options for %s was set to %s", self._name, self._attr_options
            )

    @callback
    def update_callback(self):
        """Get new data and update state."""
        self._update_zone()
        self.update_selected_zone()

    async def async_select_option(self, option: str) -> None:
        """Set next zone to be mowed."""
        _LOGGER.debug("Setting id %s to zone %s", self.api.device_id, option)
        data = {ATTR_ZONE: int(option)}
        target = {"device_id": self.api.device_id}
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_SETZONE,
            service_data=data,
            target=target,
        )


class LandroidCloudButtonBase(LandroidCloudBaseEntity, ButtonEntity):
    """Define a Landroid Cloud button class."""

    def __init__(
        self,
        description: ButtonEntityDescription,
        hass: HomeAssistant,
        api: LandroidAPI,
    ) -> None:
        """Init Landroid Cloud button."""
        super().__init__(hass, api)
        _LOGGER.debug("Initializing LandroidCloudButtonEntity for %s", api.name)
        self.entity_description = description
        self.entity_description.name = (
            f"{api.friendly_name} {description.key.capitalize()}"
        )
        self._attr_unique_id = f"{api.name}_button_{description.key}"
        self.entity_id = ENTITY_ID_FORMAT.format(self.entity_description.name)

    def press(self, **kwargs: Any) -> None:  # pylint: disable=unused-argument
        """Press the button."""
        self.hass.services.async_call(
            DOMAIN, self.api.services[self.entity_description.key]
        )


class LandroidCloudMowerBase(LandroidCloudBaseEntity, StateVacuumEntity):
    """Define a base Landroid Cloud mower class."""

    _battery_level: int | None = None
    _attr_state = STATE_INITIALIZING

    def __init__(self, hass, api):
        """Init new base device."""
        super().__init__(hass, api)
        _LOGGER.debug("Initializing LandroidCloudMowerEntity for %s", api.name)
        self.api = api
        self.hass = hass

    @property
    def extra_state_attributes(self):
        """Return sensor attributes."""
        return self._attributes

    @property
    def device_class(self) -> str:
        """Return the ID of the capability, to identify the entity for translations."""
        return f"{DOMAIN}__state"

    @property
    def robot_unique_id(self):
        """Return the unique id."""
        return f"landroid_{self._serialnumber}"

    @property
    def unique_id(self):
        """Return the unique id."""
        return self._unique_id

    @property
    def battery_level(self):
        """Return the battery level of the vacuum cleaner."""
        return self._battery_level

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self._available

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def should_poll(self):
        """Disable polling."""
        return False

    @property
    def state(self):
        """Return sensor state."""
        return self._attr_state

    @callback
    def update_callback(self):
        """Get new data and update state."""
        _LOGGER.debug("Updating mower in Home Assistant")
        self.schedule_update_ha_state(True)

    async def async_start(self):
        """Start or resume the task."""
        device: WorxCloud = self.api.device
        _LOGGER.debug("Starting %s", self._name)
        await self.hass.async_add_executor_job(device.start)

    async def async_pause(self):
        """Pause the mowing cycle."""
        device: WorxCloud = self.api.device
        _LOGGER.debug("Pausing %s", self._name)
        await self.hass.async_add_executor_job(device.pause)

    async def async_start_pause(self):
        """Toggle the state of the mower."""
        _LOGGER.debug("Toggeling state of %s", self._name)
        if STATE_MOWING in self.state:
            await self.async_pause()
        else:
            await self.async_start()

    async def async_return_to_base(self, **kwargs: Any):
        """Set the vacuum cleaner to return to the dock."""
        if self.state not in [STATE_DOCKED, STATE_RETURNING]:
            device: WorxCloud = self.api.device
            _LOGGER.debug("Sending %s back to dock", self._name)
            await self.hass.async_add_executor_job(device.home)

    async def async_stop(self, **kwargs: Any):
        """Alias for return to base function."""
        await self.async_return_to_base()

    async def async_setzone(self, service_call: ServiceCall):
        """Set next zone to cut."""
        device: WorxCloud = self.api.device
        zone = service_call.data["zone"]
        _LOGGER.debug("Setting zone for %s to %s", self._name, zone)
        await self.hass.async_add_executor_job(partial(device.setzone, str(zone)))

    async def async_set_schedule(self, service_call: ServiceCall):
        """Set or change the schedule."""
        device: WorxCloud = self.api.device
        schedule_type = service_call.data["type"]
        _LOGGER.debug(SCHEDULE_TYPE_MAP[schedule_type])
        schedule = {}
        if schedule_type == "secondary":
            # We are handling a secondary schedule
            # Insert primary schedule in dataset befor generating secondary
            schedule[SCHEDULE_TYPE_MAP["primary"]] = pass_thru(
                device.schedules["primary"]
            )

        schedule[SCHEDULE_TYPE_MAP[schedule_type]] = []
        _LOGGER.debug(json.dumps(schedule))
        _LOGGER.debug("Generating %s schedule", schedule_type)
        for day in SCHEDULE_TO_DAY.items():
            day = day[1]
            if day["start"] in service_call.data:
                # Found day in dataset, generating an update to the schedule
                if not day["end"] in service_call.data:
                    raise HomeAssistantError(
                        f"No end time specified for {day['clear']}"
                    )
                schedule[SCHEDULE_TYPE_MAP[schedule_type]].append(
                    parseday(day, service_call.data)
                )
            else:
                # Didn't find day in dataset, parsing existing thru
                current = []
                current.append(device.schedules[schedule_type][day["clear"]]["start"])
                current.append(
                    device.schedules[schedule_type][day["clear"]]["duration"]
                )
                current.append(
                    int(device.schedules[schedule_type][day["clear"]]["boundary"])
                )
                schedule[SCHEDULE_TYPE_MAP[schedule_type]].append(current)

        if schedule_type == "primary":
            # We are generating a primary schedule
            # To keep a secondary schedule we need to pass this thru to the dataset
            schedule[SCHEDULE_TYPE_MAP["secondary"]] = pass_thru(
                device.schedules["secondary"]
            )

        data = json.dumps({"sc": schedule})
        _LOGGER.debug(
            "New %s schedule, %s, sent to %s", schedule_type, data, self._name
        )
        await self.hass.async_add_executor_job(partial(device.send, data))
