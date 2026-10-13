# Moving from V3

V3 can remain installed while you inspect V4. V4 has a distinct add-on slug and MQTT unique IDs. It cannot automatically take over V3 entity IDs because V3 creates Home Assistant REST states while V4 creates MQTT entities.

1. Record dashboards and automations that reference V3 states or its input_text settings helper.
2. Install V4 and choose the same inverter in **Setup → Select Inverters**. Check its MQTT device in Home Assistant.
3. Replace references one at a time with the V4 entities. Match by measurement and unit; not every V3 field has a V4 equivalent.
4. Stop V3 publication before relying on V4 for the same workflow. Keep V3 installed until migration is complete.
5. Remove old V3 helpers only after automations have moved.

No automatic entity-ID preservation is promised. V4's inverter and PV total energy values correspond to V3's inverter info etotal and PV input etotal values respectively; verify each displayed value before use in the Energy dashboard.
