# RTKey for Home Assistant

This integration creates lock, image, and camera entities for intercoms from key.rt.ru.

## Install with HACS

1. In HACS, open **Custom repositories** from the menu.
2. Enter `https://github.com/taran507/hass_rtkey` and select **Integration**.
3. Download the integration and restart Home Assistant.
4. Add the RTKey integration in **Settings > Devices & services**. Enter your phone number, password, and device ID.

The integration obtains an access token automatically and keeps it in memory until renewal.