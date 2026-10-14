# SolarSynkV4

**Sunsynk cloud monitoring and inverter controls for Home Assistant, with automatic inverter discovery and MQTT entities.**

SolarSynkV4 connects to your Sunsynk or Inteless cloud account, lets you choose which inverters to monitor, and publishes their readings and supported controls to Home Assistant through MQTT. A dedicated sidebar interface provides setup, monitoring, settings and activity logs.

It is a **Home Assistant add-on/app**, rather than a HACS integration. Your existing dongle continues to send data to the vendor cloud; no additional inverter cable is required.

> **Release status: release candidate, version 0.1.15.** Automated unit tests are documented, but container builds, Home Assistant ingress, live cloud authentication, real MQTT operation and physical inverter settings changes have not yet been validated in the repository's documented test environment. This is not a production-validated release. See [validation status](docs/TECHNICAL.md) and the [release acceptance checklist](docs/RELEASE_VALIDATION.md).

## Features

- **Automatic inverter discovery:** find plants and inverters in your cloud account, then select the devices you want. No manual serial-number entry.
- **Home Assistant MQTT discovery:** create devices, telemetry sensors and supported number, switch and text controls.
- **Cloud monitoring:** solar, battery, grid, load and other available readings, including supported MPPT and phase data.
- **Settings controls:** supported battery, system mode, six-slot schedules, Aux Load and selected advanced settings.
- **Pending-value handling:** keep your requested setting visible while the cloud still reports an older value.
- **Grouped settings writes:** read settings before a write, preserve unchanged fields and outstanding requested values, and refuse a write if a mandatory group field is missing.
- **Read-back confirmation:** check the cloud after sending a setting, for up to three minutes.
- **Connection and activity information:** inspect cloud fetches, MQTT events and setting outcomes.
- **Saved configuration:** retain account settings, discovered devices and inverter selections across restarts.
- **MQTT recovery:** republish discovery and cached states after reconnecting, with checks to distinguish stale telemetry.
- **Sidebar setup:** configure cloud and MQTT connections through the app interface.

Available readings and controls depend on what your inverter model, firmware and cloud account report. The project does not claim compatibility with every Sunsynk or Deye model.

## Requirements

- Home Assistant with add-on/app support.
- A cloud-connected inverter whose data is visible in Sunsynk Connect or the applicable Inteless service.
- Cloud account credentials; permissions to change inverter settings are needed for controls.
- An MQTT broker and the Home Assistant MQTT integration with discovery enabled.
- Internet access from both the dongle and Home Assistant.
- An `amd64` or `aarch64` host, as declared in the current add-on configuration.

MFA authentication is not currently supported or validated. Inteless support is implemented but has not been live-validated in the documented environment.

## Installation

1. In Home Assistant, open **Settings → Apps → App store**, or **Settings → Add-ons → Add-on Store** on versions using the older names.
2. Open the **⋮** menu and select **Repositories**.
3. Add this repository URL:

   ```text
   https://github.com/martinville/SolarSynkV4
   ```

4. Close the repository dialog and find **SolarSynkV4** in the store. If it is not listed yet, choose **Check for updates** from the store menu.
5. Open **SolarSynkV4** and select **Install**.
6. Enable **Start on boot** and **Show in sidebar**, then select **Start**.
7. Select **Open Web UI**, or open **SolarSynkV4** from the sidebar, to complete setup.

Home Assistant builds the app image during installation. If installation fails, inspect the installation logs and report the error through GitHub Issues.

## First-time setup

1. Open **Setup → Setup MQTT**.
2. Choose **Automatic** to request broker details from Home Assistant, or **Manual** for an external broker. Test the connection, then save.
3. Open **Setup → Cloud Setup**. Enter your email and password, choose **Sunsynk** or **Inteless**, and save.
4. Open **Setup → Select Inverters**. Select **Refresh inverter list**, check the devices you want, and save the selection.
5. Open **Overview** and check cloud status, MQTT status and telemetry timestamps.
6. In Home Assistant, open **Settings → Devices & services → MQTT** and find the discovered inverter device.

MQTT is required to bring V4's devices and controls into Home Assistant. A successful cloud fetch can still contain old dongle data; always check the telemetry timestamp.

## Monitoring

Expand an inverter card on **Overview** to see available readings, units and stable MQTT unique IDs. Home Assistant may assign different visible entity IDs.

Select **Send all data to MQTT** to request a cloud refresh and republish discovery and states. This does not force the dongle to upload a new sample.

Polling is configurable from 60 to 1,800 seconds. Faster cloud polling does not guarantee fresher inverter readings: the dongle's upload interval and cloud processing determine when new data becomes available.

## Changing inverter settings

Use either:

- The **Inverter Settings** page and its **Save changes** button.
- The normal controls on the discovered Home Assistant entities.
- Home Assistant actions such as `number.set_value`, `switch.turn_on`, `switch.turn_off` or `text.set_value` for schedule times in `HH:MM` format.

Home Assistant Developer Tools **Set state** only changes the displayed state; it does not send a command to the inverter.

### What happens after a change?

| Status | Meaning |
| --- | --- |
| Pending | Your requested value is queued for sending. |
| Sent | The cloud accepted the request; V4 is waiting for read-back. |
| Confirmed | A later cloud settings read matched the requested value. |
| Rejected | Validation, required data or the API prevented the write. |
| Unconfirmed | Cloud read-back did not confirm the value within three minutes. |

While a change is pending, the requested value remains visible instead of switching back and forth with older cloud readings. Later grouped writes preserve outstanding requested values.

**Confirmed means cloud read-back matched. It does not independently prove that the physical inverter applied the setting.** During validation, check the vendor portal and the inverter itself.

### Current control limits

- Controls are exposed only when their fields are reported by the cloud.
- Missing mandatory settings-group fields block a write.
- Settings writes are blocked when the returned settings indicate parallel operation; master/slave routing is not implemented.
- Grid and parallel topology controls are excluded.
- Control ranges are predefined and need model-specific validation.
- Unknown scalar cloud settings may appear as read-only diagnostics.
- Pending commands are not persisted for replay after a restart.

## Connection problems

| Symptom | First check |
| --- | --- |
| Cloud sign-in fails | Account credentials, selected cloud service, connectivity and MFA status. |
| No inverter appears | Refresh the inverter list and confirm the account can access the device in the vendor portal. |
| No HA entities appear | MQTT connection, Home Assistant MQTT integration and discovery settings. |
| Readings are old | Telemetry timestamp and dongle upload status. |
| A setting is rejected | Activity logs, account permissions, parallel status and missing group fields. |
| A setting is unconfirmed | Portal and inverter values; the API may acknowledge a request before it is applied. |

Use **Activity** for recent events. See [Troubleshooting](docs/TROUBLESHOOTING.md) for more detail.

## Updating and migrating

In Home Assistant, open the App/Add-on Store and choose **Check for updates**. When a new version is available, open **SolarSynkV4** and select **Update**. Restarting alone does not install a new version.

SolarSynkV4 uses its own MQTT devices and unique IDs. Existing SolarSynkV3 entity references, dashboards and automations may need updating.

See [Migration](docs/MIGRATION.md) and [Dashboard notes](docs/DASHBOARDS.md).

## Validation and development

The documented local checks include Python compilation and 48 unit tests covering discovery, persistence, telemetry, MQTT behavior and settings-write handling.

Run the local release gate from the repository root:

```bash
python tools/release_check.py
```

This checks syntax, version alignment and the unit suite. It does not build the container or validate live operation.

Before claiming production readiness, complete the [release acceptance checklist](docs/RELEASE_VALIDATION.md) and publish results identifying:

- Inverter model and firmware.
- Cloud service and Home Assistant version.
- Host architecture.
- Settings groups tested.
- Cloud read-back and physical inverter results.
- Reconnect, outage and restart behavior.

## Documentation

- [Installation and first run](docs/INSTALL.md)
- [Daily use](docs/DAILY_USE.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Migration from V3](docs/MIGRATION.md)
- [Dashboards](docs/DASHBOARDS.md)
- [Technical notes and validation status](docs/TECHNICAL.md)
- [Implemented API endpoints](docs/API_ENDPOINTS.md)
- [Release acceptance checklist](docs/RELEASE_VALIDATION.md)

## Feedback

Report problems through [GitHub Issues](https://github.com/martinville/SolarSynkV4/issues). Include your app version, Home Assistant version, inverter model, firmware, cloud service and steps to reproduce the issue.

Remove passwords, tokens, account details and other private information from logs or API responses before sharing them.

SolarSynkV4 is an independent community project and is not affiliated with or endorsed by Sunsynk, Deye or Home Assistant.
