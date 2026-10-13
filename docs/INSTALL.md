# Installation and first run

SolarSynkV4 needs Home Assistant with add-on support, a Sunsynk/Inteless cloud account, a cloud-connected inverter, and an MQTT broker. MQTT carries V4's devices and values into Home Assistant. [Home Assistant's MQTT instructions](https://www.home-assistant.io/integrations/mqtt/) explain the broker and integration.

This is a release candidate. Test on a noncritical installation first. No real account, broker, or Home Assistant installation was available for validation in this checkout.

1. This checkout has not been published as a Home Assistant repository. For local validation, place the `solarsynkv4` folder in Home Assistant's `/local_apps` directory, choose **Check for updates** in the App store, then install **SolarSynkV4** from Local apps. Once the repository is published, add its URL through the Add-on Store menu instead. The add-on Configuration tab has no application settings.
2. Start the add-on and open **SolarSynkV4** in the sidebar.
3. Open **Setup → Setup MQTT**. In the popup, choose **Automatic** to use broker details from Home Assistant, or **Manual** to enter an external broker. Select **Test connection**, then **Save MQTT settings**. The popup shows the result and connection status.
4. Open **Setup → Cloud Setup**. Enter **Cloud account email** and **Cloud password**, choose **Cloud service**, and choose whether to use **Extra login**. Leave **Polling** at its default unless needed. Select **Save settings**. Cloud sign-in is checked before changes are saved.
5. Open **Setup → Select Inverters**. In the popup, select **Refresh inverter list**. The list is saved and will still be there after a restart. Refresh again only after adding an inverter to the cloud account.
6. In the same popup, check only the devices to monitor and select **Save selected inverters**. Your choices stay saved independently of the discovered list. You do not enter serial numbers. A cloud fetch starts after you save the selection. The inverter may still report an older telemetry timestamp.
7. Open **Overview**. Confirm that Cloud and MQTT show Connected, the selected inverter count is correct, and the inverter card shows a recent telemetry time. Expand the card to view its entities. Then open Home Assistant **Settings → Devices & services → MQTT** and find the discovered device.


The Cloud Setup popup preserves the saved cloud password, and the MQTT popup preserves a saved manual broker password when its field is left blank. A failed sign-in leaves the saved account unchanged. Setup can be resumed after leaving the page.

## Updating an existing installation

The source files in this workspace do not change an app already installed in Home Assistant. This release is `0.1.15`.

- **Local app:** Copy the updated `solarsynkv4` directory, including `config.yaml`, into the same Home Assistant local apps directory used for the existing installation (currently `/local_apps/solarsynkv4` in Home Assistant's documentation). In **Settings → Apps → App store**, choose **Check for updates**, then open SolarSynkV4 and select **Update**. Start it if Home Assistant does not start it automatically.
- **Repository install:** Publish the changed repository, including the `0.1.15` version in `config.yaml`, to the repository Home Assistant uses. Then choose **Check for updates** in the App store and **Update** SolarSynkV4.

A plain **Restart** uses the already installed image and does not copy or build this source. After the updated app starts, its next cloud settings poll will refresh MQTT discovery; **Send all data to MQTT** requests that refresh immediately.
