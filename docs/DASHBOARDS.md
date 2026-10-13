# Add devices to Home Assistant dashboards

1. After completing setup, open **Settings → Devices & services → MQTT** in Home Assistant.
2. Find the device named after your inverter and open it. Its sensors and available settings controls are listed there.
3. To add a card, open a dashboard, select **Edit dashboard**, select **Add card**, choose **Entities**, and select the SolarSynkV4 entities you want.
4. For the Energy dashboard, use a lifetime energy sensor only if it appears with the expected kWh value and **total increasing** state. This preview maps PV total, grid import/export total, and battery charge/discharge total energy when those fields are present. Confirm each unit and value against the vendor portal before adding it.

See [Home Assistant's Energy documentation](https://www.home-assistant.io/docs/energy/) for dashboard setup.
