# Production acceptance for 0.1.15

Run these checks on a test Home Assistant installation and a test inverter before using settings writes on an operational system. Record the model, firmware, cloud service, test date, and result for each check.

1. Build and install the add-on image on both supported architectures (`amd64` and `aarch64`). Open its ingress page, restart it, and confirm saved settings and selected inverters remain available.
2. Connect MQTT in Automatic and Manual modes. Confirm each selected inverter appears as a device with its saved name, renaming it updates the device name without changing its entities, discovery survives broker and app restarts, and removed inverters lose their discovery entities.
3. Set polling to 60 seconds. Confirm successive cloud fetch attempts follow the saved interval and that a saved interval change takes effect without restarting the app.
4. On a test inverter, change one reversible Battery, System mode, Aux Load, and Advanced control through Home Assistant. For each, confirm the Activity and add-on logs show `Sunsynk API write succeeded`, the command later becomes `confirmed`, and the portal and inverter display the requested value. Record the full POST and read-back response with credentials and tokens removed.
5. Change two controls quickly on one page, then change a shared control across two pages. Confirm the later POST preserves the earlier requested value. Test a rejected API response and a read-back timeout; neither may appear as a confirmed inverter change.
6. Test wrong-inverter responses, cloud outage, MQTT outage, and restart during a pending write. Confirm no command is sent to another serial and no old command is replayed on restart.
7. Repeat the write checks for every inverter model, firmware version, and cloud service intended for deployment. Grid and parallel topology controls remain excluded.

The local release gate is `python tools/release_check.py` from the `solarsynkv4` directory. It checks Python syntax, metadata version alignment, and the unit suite. It does not build an image or replace these live checks.
