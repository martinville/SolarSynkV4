# Troubleshooting

## MQTT says Disconnected

1. Confirm that the Mosquitto broker is running and the MQTT integration is configured. Follow [Home Assistant's MQTT guide](https://www.home-assistant.io/integrations/mqtt/).
2. Open **Setup → Setup MQTT**. Choose **Automatic**, select **Test connection**, then **Save MQTT settings**. V4 asks Supervisor for broker details again. If automatic details are unavailable, check that the broker app is running and offers an MQTT service to other apps. The MQTT integration or an external broker alone does not provide Supervisor service details. Manual mode can still work; keep Manual selected when Automatic is unavailable. The popup now checks Supervisor each time it opens and shows the specific lookup result.

## MQTT connects but no inverter device appears

1. Open **Overview** and confirm the inverter is selected. If needed, use **Setup → Select Inverters** to select it. V4 now creates a diagnostic Telemetry status entity for each selected inverter before its first cloud fetch.
2. Open **Activity**. Look for **Connected to the MQTT broker**, **Queued ... Home Assistant discovery configuration(s)**, and **Queued ... telemetry value(s)**. If the cloud fetch succeeded while MQTT was disconnected, V4 resends those values after MQTT reconnects.
3. In Home Assistant, open **Settings → Devices & services → MQTT** and confirm the MQTT integration is configured and discovery is enabled. A successful broker connection alone does not create Home Assistant devices.
4. If Activity shows queued discovery but Home Assistant still has no device, check the MQTT integration log for a rejected discovery payload. Do not share broker passwords or raw cloud responses.

## Cloud sign-in fails

1. In **Setup → Cloud Setup**, check **Cloud service** and **Cloud account email**.
2. Re-enter **Cloud password** and select **Save settings**. Some MFA accounts may not work with the V3-derived login flow.
3. If the cloud is down or rate limiting, wait and retry.

## No inverter appears

1. Open **Setup → Select Inverters** and select **Refresh inverter list** in the popup. Previously saved devices stay listed if a refresh fails.
2. Confirm the account can see the plant in the vendor portal. A shared plant may have limited permissions.
3. If a plant fails, discovery shows its error; accessible plants may still appear.

## A value looks old

Compare **Inverter telemetry** with **Last API update** on **Overview**. The API can answer successfully with older inverter data. Check the inverter's cloud connection.

## A setting did not change

Open **Activity**. An unconfirmed change was accepted by the API but not seen in read-back within three minutes. Check the vendor portal before trying again. Parallel systems are blocked from writes when V4 detects a nonzero parallel role.

## Extra login password lost

An authorised Home Assistant administrator can stop the add-on, back up /data/config.json through an authorised file access method, then set admin.enabled to false and clear admin.hash in that file. Restart and set a new password in **Setup → Cloud Setup**. There is no unauthenticated reset endpoint. This procedure has not been exercised on a live Home Assistant host.
