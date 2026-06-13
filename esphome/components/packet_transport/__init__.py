"""ESPHome packet transport component."""

import hashlib
import logging

from esphome import automation
import esphome.codegen as cg
from esphome.components.api import CONF_ENCRYPTION
from esphome.components.binary_sensor import BinarySensor
from esphome.components.sensor import Sensor
import esphome.config_validation as cv
from esphome.const import (
    CONF_BINARY_SENSOR,
    CONF_BINARY_SENSORS,
    CONF_ID,
    CONF_INTERNAL,
    CONF_KEY,
    CONF_NAME,
    CONF_PLATFORM,
    CONF_SENSOR,
    CONF_SENSORS,
)
from esphome.core import CORE
from esphome.cpp_generator import MockObjClass

CODEOWNERS = ["@clydebarrow"]
AUTO_LOAD = ["xxtea"]

packet_transport_ns = cg.esphome_ns.namespace("packet_transport")
PacketTransport = packet_transport_ns.class_("PacketTransport", cg.PollingComponent)

AddProviderAction = packet_transport_ns.class_("AddProviderAction", automation.Action)
RemoveProviderAction = packet_transport_ns.class_(
    "RemoveProviderAction", automation.Action
)
SetProviderAction = packet_transport_ns.class_("SetProviderAction", automation.Action)

byte_vector = cg.std_vector.template(cg.uint8)

IS_PLATFORM_COMPONENT = True

DOMAIN = "packet_transport"
CONF_BROADCAST = "broadcast"
CONF_BROADCAST_ID = "broadcast_id"
CONF_ENCRYPTION_KEY = "encryption_key"
CONF_PROVIDER = "provider"
CONF_PROVIDERS = "providers"
CONF_REMOTE_ID = "remote_id"
CONF_PING_PONG_ENABLE = "ping_pong_enable"
CONF_PING_PONG_RECYCLE_TIME = "ping_pong_recycle_time"
CONF_ROLLING_CODE_ENABLE = "rolling_code_enable"
CONF_TRANSPORT_ID = "transport_id"


_LOGGER = logging.getLogger(__name__)


def sensor_validation(cls: MockObjClass):
    return cv.maybe_simple_value(
        cv.Schema(
            {
                cv.Required(CONF_ID): cv.use_id(cls),
                cv.Optional(CONF_BROADCAST_ID): cv.validate_id_name,
            }
        ),
        key=CONF_ID,
    )


def provider_name_validate(value):
    value = cv.valid_name(value)
    if "_" in value:
        _LOGGER.warning(
            "Device names typically do not contain underscores - did you mean to use a hyphen in '%s'?",
            value,
        )
    return value


ENCRYPTION_SCHEMA = {
    cv.Optional(CONF_ENCRYPTION): cv.maybe_simple_value(
        cv.Schema(
            {
                cv.Required(CONF_KEY): cv.string,
            }
        ),
        key=CONF_KEY,
    )
}

PROVIDER_SCHEMA = cv.Schema(
    {
        cv.Required(CONF_NAME): provider_name_validate,
    }
).extend(ENCRYPTION_SCHEMA)


def validate_(config):
    if CONF_ENCRYPTION in config:
        if CONF_SENSORS not in config and CONF_BINARY_SENSORS not in config:
            raise cv.Invalid("No sensors or binary sensors to encrypt")
    elif config[CONF_ROLLING_CODE_ENABLE]:
        raise cv.Invalid("Rolling code requires an encryption key")
    if config[CONF_PING_PONG_ENABLE] and not any(
        CONF_ENCRYPTION in p for p in config.get(CONF_PROVIDERS) or ()
    ):
        raise cv.Invalid("Ping-pong requires at least one encrypted provider")
    return config


TRANSPORT_SCHEMA = (
    cv.polling_component_schema("15s")
    .extend(
        {
            cv.Optional(CONF_ROLLING_CODE_ENABLE, default=False): cv.boolean,
            cv.Optional(CONF_PING_PONG_ENABLE, default=False): cv.boolean,
            cv.Optional(
                CONF_PING_PONG_RECYCLE_TIME, default="600s"
            ): cv.positive_time_period_seconds,
            cv.Optional(CONF_SENSORS): cv.ensure_list(sensor_validation(Sensor)),
            cv.Optional(CONF_BINARY_SENSORS): cv.ensure_list(
                sensor_validation(BinarySensor)
            ),
            cv.Optional(CONF_PROVIDERS, default=[]): cv.ensure_list(PROVIDER_SCHEMA),
        },
    )
    .extend(ENCRYPTION_SCHEMA)
    .add_extra(validate_)
)


def transport_schema(cls):
    return TRANSPORT_SCHEMA.extend({cv.GenerateID(): cv.declare_id(cls)})


def get_sensors(transport_id):
    """Return the list of sensors for this platform."""
    return (
        sensor
        for sensor in CORE.data.setdefault(DOMAIN, {}).setdefault(CONF_SENSORS, [])
        if sensor[CONF_TRANSPORT_ID] == transport_id
    )


def validate_packet_transport_sensor(config):
    if CONF_NAME in config and CONF_INTERNAL not in config:
        raise cv.Invalid("Must provide internal: config when using name:")
    conf_sensors = CORE.data.setdefault(DOMAIN, {}).setdefault(CONF_SENSORS, [])
    conf_sensors.append(config)
    return config


def packet_transport_sensor_schema(base_schema):
    return cv.All(
        base_schema.extend(
            {
                cv.GenerateID(CONF_TRANSPORT_ID): cv.use_id(PacketTransport),
                cv.Optional(CONF_REMOTE_ID): cv.string_strict,
                cv.Required(CONF_PROVIDER): provider_name_validate,
            }
        ),
        cv.has_at_least_one_key(CONF_ID, CONF_REMOTE_ID),
        validate_packet_transport_sensor,
    )


def hash_encryption_key(config: dict):
    return list(hashlib.sha256(config[CONF_KEY].encode()).digest())


async def register_packet_transport(var, config):
    var = await cg.register_component(var, config)
    cg.add(var.set_rolling_code_enable(config[CONF_ROLLING_CODE_ENABLE]))
    cg.add(var.set_ping_pong_enable(config[CONF_PING_PONG_ENABLE]))
    cg.add(
        var.set_ping_pong_recycle_time(
            config[CONF_PING_PONG_RECYCLE_TIME].total_seconds
        )
    )
    # Get directly configured providers, plus those from sensors and binary sensors
    providers = {
        sensor[CONF_PROVIDER] for sensor in get_sensors(config[CONF_ID])
    }.union(x[CONF_NAME] for x in config[CONF_PROVIDERS])
    for provider in providers:
        cg.add(var.add_provider(provider))
    for provider in config[CONF_PROVIDERS]:
        name = provider[CONF_NAME]
        if encryption := provider.get(CONF_ENCRYPTION):
            cg.add(var.set_provider_encryption(name, hash_encryption_key(encryption)))

    is_provider = False
    sensors = config.get(CONF_SENSORS, ())
    binary_sensors = config.get(CONF_BINARY_SENSORS, ())
    if sensors:
        cg.add(var.set_sensor_count(len(sensors)))
    if binary_sensors:
        cg.add(var.set_binary_sensor_count(len(binary_sensors)))
    for sens_conf in sensors:
        is_provider = True
        sens_id = sens_conf[CONF_ID]
        sensor = await cg.get_variable(sens_id)
        bcst_id = sens_conf.get(CONF_BROADCAST_ID, sens_id.id)
        cg.add(var.add_sensor(bcst_id, sensor))
    for sens_conf in binary_sensors:
        is_provider = True
        sens_id = sens_conf[CONF_ID]
        sensor = await cg.get_variable(sens_id)
        bcst_id = sens_conf.get(CONF_BROADCAST_ID, sens_id.id)
        cg.add(var.add_binary_sensor(bcst_id, sensor))

    if is_provider:
        cg.add(var.set_is_provider(True))
    if encryption := config.get(CONF_ENCRYPTION):
        cg.add(var.set_encryption_key(hash_encryption_key(encryption)))
    return providers


async def new_packet_transport(config):
    var = cg.new_Pvariable(config[CONF_ID])
    cg.add(var.set_platform_name(config[CONF_PLATFORM]))
    providers = await register_packet_transport(var, config)
    return var, providers


# ============================================ Actions ============================================


def _validate_encryption_key(value):
    value = cv.ensure_list(cv.hex_uint8_t)(value)
    if len(value) != 32:
        raise cv.Invalid(
            f"Encryption key must be exactly 32 bytes (got {len(value)}). "
            "Provide the already-derived key used by the peer."
        )
    return value


ADD_PROVIDER_ACTION_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.use_id(PacketTransport),
        cv.Required(CONF_NAME): cv.templatable(provider_name_validate),
        cv.Optional(CONF_ENCRYPTION_KEY): cv.templatable(_validate_encryption_key),
    }
)


@automation.register_action(
    "packet_transport.add_provider", AddProviderAction, ADD_PROVIDER_ACTION_SCHEMA
)
async def add_provider_action_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    templ = await cg.templatable(config[CONF_NAME], args, cg.std_string)
    cg.add(var.set_name(templ))
    if CONF_ENCRYPTION_KEY in config:
        key = await cg.templatable(
            config[CONF_ENCRYPTION_KEY], args, byte_vector, byte_vector
        )
        cg.add(var.set_encryption_key(key))
    return var


REMOVE_PROVIDER_ACTION_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.use_id(PacketTransport),
        cv.Required(CONF_NAME): cv.templatable(provider_name_validate),
    }
)


@automation.register_action(
    "packet_transport.remove_provider",
    RemoveProviderAction,
    REMOVE_PROVIDER_ACTION_SCHEMA,
)
async def remove_provider_action_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    templ = await cg.templatable(config[CONF_NAME], args, cg.std_string)
    cg.add(var.set_name(templ))
    return var


SET_PROVIDER_ACTION_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.use_id(PacketTransport),
            cv.Optional(CONF_SENSOR): cv.use_id(Sensor),
            cv.Optional(CONF_BINARY_SENSOR): cv.use_id(BinarySensor),
            cv.Required(CONF_PROVIDER): cv.templatable(provider_name_validate),
        }
    ),
    cv.has_exactly_one_key(CONF_SENSOR, CONF_BINARY_SENSOR),
)


@automation.register_action(
    "packet_transport.set_provider", SetProviderAction, SET_PROVIDER_ACTION_SCHEMA
)
async def set_provider_action_to_code(config, action_id, template_arg, args):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    templ = await cg.templatable(config[CONF_PROVIDER], args, cg.std_string)
    cg.add(var.set_provider(templ))
    if CONF_SENSOR in config:
        sens = await cg.get_variable(config[CONF_SENSOR])
        cg.add(var.set_sensor(sens))
    if CONF_BINARY_SENSOR in config:
        sens = await cg.get_variable(config[CONF_BINARY_SENSOR])
        cg.add(var.set_binary_sensor(sens))
    return var
