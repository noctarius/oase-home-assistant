"""Constants for the OASE Control integration."""

from datetime import timedelta

DOMAIN = "oase_control"
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
PLATFORMS = ["binary_sensor", "light", "sensor", "switch"]
UPDATE_INTERVAL = timedelta(seconds=30)

