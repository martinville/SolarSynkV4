# Technical notes and validation status

## API inventory

See [the complete V4 endpoint inventory](API_ENDPOINTS.md) for every path currently called, request fields, response shapes and source evidence. This is the inventory of implemented paths, not an enumeration of every private vendor endpoint. No live account validation was available here.

## Competitor feature matrix (source review)

| Capability | V3 | Built-in HA Sunsynk (2026.10) | MarcinG81 integration | V4 preview |
| --- | --- | --- | --- | --- |
| Cloud telemetry | Yes, broad per-field REST posting | Yes, focused solar/grid/battery/load | Yes, broad including MPPT and phases | V3 source-confirmed telemetry paths and selected safe fields |
| Writable settings | V3 helper string and partial group templates | No | Yes, native HA entities and group writes | Selected MQTT entities, guarded complete groups |
| Multiple inverters | Manual serial list | Automatic all account inverters | Manual serial list | Paginated plant discovery and selection |
| HA device discovery | No, REST states | Native integration | Native integration | MQTT discovery |
| Delayed write confirmation | No reliable read-back | N/A | Fresh read-back, documented queue handling | Pending value and bounded cloud read-back, fixture tests |
| Recovery and diagnostics | Logs | HA repairs | HA repairs and diagnostics | UI activity and error summaries; incomplete |
| Ease of use | Add-on options and helper commands | Strong native setup | Native setup but asks serial numbers | Sidebar setup, no serial entry; usability untested |

Sources: [V3 code](https://github.com/martinville/solarsynkv3), [built-in integration documentation](https://www.home-assistant.io/integrations/sunsynk), [MarcinG81 source](https://github.com/MarcinG81/SunSynk_HA_Integration). V4 cannot claim overall superiority. The built-in integration is more mature for monitoring and the MarcinG81 integration currently covers more settings.

## Implemented changes relative to V3

- Separate add-on slug and ingress-only configuration.
- Automatic paginated plant and inverter listing, selection, and per-inverter failure isolation.
- MQTT discovery with per-device availability and retained configuration, rather than REST-created states. A selected inverter gets a diagnostic entity before its first cloud sample; cached states are replayed after reconnect without marking stale samples fresh.
- Reused cloud token, certificate verification, atomic /data configuration, and redacted UI output.
- Shared Web UI and MQTT settings queue with coalescing, immediate MQTT writes, outstanding-value carryover, and bounded cloud read-back.

## Architecture

Store is app/store.py; cloud login and transport are app/cloud.py; discovery is app/discovery.py; telemetry is app/telemetry.py; settings queue is app/settings.py; MQTT is app/mqtt.py; web UI and poll loop are app/main.py. The container uses a single Gunicorn worker with four threads because MQTT, sessions and the polling loop currently share process memory. No browser JavaScript is used. Forms use relative ingress paths. MQTT service information is read from the Supervisor /services/mqtt endpoint using SUPERVISOR_TOKEN when the MQTT popup is set to Automatic. The add-on declares mqtt:want. Manual settings in the MQTT popup are a fallback.

## Validation performed

- Python compileall passed on all app and test files.
- Forty-eight unittest cases passed for pagination, missing telemetry, persistence, group preservation, delayed confirmation, supersession, rejection and timeout.
- Container build was not run because Docker and Podman are absent in this environment. Local Flask/Gunicorn runtime checks were blocked because PyPI access was denied and those packages are not installed here.
- Home Assistant ingress, MQTT broker, cloud login, Inteless login, device-specific settings writes and physical application were not validated. No live credentials were available.

For a step-by-step sign-off, use [Production acceptance](RELEASE_VALIDATION.md).

## Required before production use

1. Build the add-on image on each supported architecture and install it in a real Home Assistant instance.
2. Verify the complete first-run flow through ingress on desktop and mobile, including login cookies and Supervisor MQTT service discovery.
3. With a test inverter account on each cloud service, inspect actual paginated plant/inverter responses and telemetry field names, including shared plants.
4. For each inverter model and setting group, compare the V4 POST payload with the vendor portal's accepted payload, test permission and parallel roles, then confirm cloud read-back and physical inverter application separately.
5. Run broker reconnect, retained discovery cleanup and command replay checks with a real MQTT broker.
6. Extend fixtures and tests to authentication, ingress, sessions and all write paths before claiming production readiness.

## Overview and manual MQTT refresh

Overview counts selected inverters, V4-owned MQTT discovery records, fresh inverter samples, and uptime since process start. The entity table shows stable MQTT unique IDs rather than claiming Home Assistant registry entity IDs. The manual **Send all data to MQTT** action uses the same cloud polling and normalization path as the scheduled cycle, reads available settings, and republishes retained V4 discovery configs. It sends state for normalized telemetry, supported controls and safe scalar cloud settings exposed as read-only diagnostics. It excludes known secret fields and does not invent units for unknown fields. One refresh runs at a time, and selected inverters are handled independently.

The UI includes a local copy of Bootstrap 5.3.8 CSS in `app/static/bootstrap.min.css`; its MIT license is in `app/static/BOOTSTRAP_LICENSE.txt`. Dark theme rules are embedded in the Python view and do not require a CDN or JavaScript.

## Inverter list persistence

Cloud discovery results are cached in `/data/config.json` by serial number. Refresh merges new and updated devices with the cached list and does not change the separately persisted selected serials. A failed or partial refresh leaves previously known inverters visible. Existing installations with selected serials and no cached list are migrated to placeholder list entries so the user can still see and deselect them. Cloud Setup, Select Inverters and Setup MQTT are native HTML popups opened from the Bootstrap navbar Setup dropdown. The MQTT popup has a connection test that performs an MQTT CONNECT handshake without saving.
