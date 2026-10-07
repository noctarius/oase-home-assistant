"""Constants for the OASE Control integration."""

from datetime import timedelta

DOMAIN = "oase_control"
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_LOCAL_CREDENTIALS = "local_credentials"
PLATFORMS = ["binary_sensor", "light", "number", "sensor", "switch"]
UPDATE_INTERVAL = timedelta(seconds=30)
