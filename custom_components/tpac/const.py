"""Constants for the TPAC integration."""

from datetime import timedelta

DOMAIN = "tpac"
EVENT_TPAC = "tpac_event"
# The TPAC device as Home Assistant shows it.
HUB_MODEL = "TPAC"
HUB_MANUFACTURER = "@janlay"
HUB_URL = "https://github.com/janlay/tpac"
DEFAULT_INTERFACE = "enp0s1"
CONF_INTERFACE = "interface"
UPDATE_INTERVAL = timedelta(seconds=15)
# The AC identity that access points see in Discovery and Join responses.
AC_NAME = "TL-R470GP-AC"
AC_FIRMWARE = "1.3.0 Build 260520 Rel.57841"
