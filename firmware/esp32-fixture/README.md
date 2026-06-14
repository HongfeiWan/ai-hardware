# ESP32 Fixture MCP

This is the first deployable ESP32-side MCP firmware for the AI Hardware project. It runs a small HTTP MCP server on the ESP32 and exposes only allowlisted fixture actions.

Default mode is SoftAP so the firmware can be used without a lab router:

- SSID: `ai-hardware-fixture-XXXX`
- Password: `aihardware`
- ESP32 IP: `192.168.4.1`
- MCP endpoint: `http://192.168.4.1/mcp`

## Tools

| Tool | Purpose |
| --- | --- |
| `fixture.ping` | Health check |
| `fixture.get_status` | Read IP, uptime, GPIO mapping, MUX channel and load state |
| `fixture.self_test` | Run non-destructive GPIO, ADC and heap sanity checks |
| `fixture.set_mux_channel` | Select an allowlisted MUX channel |
| `fixture.select_net` | Select a MUX channel by configured net/testpoint label |
| `fixture.reset_dut` | Pulse DUT reset within configured max duration |
| `fixture.set_load_switch` | Enable/disable a fixture-controlled load switch |
| `fixture.read_digital_input` | Read one configured digital input such as PGOOD, FAULT or IRQ |
| `fixture.scan_digital_inputs` | Read all configured digital inputs |
| `fixture.read_adc_raw` | Read averaged ADC samples from the configured ADC channel |
| `fixture.read_net_adc_raw` | Select a configured net/testpoint, wait for MUX settling and read ADC samples |
| `fixture.scan_net_adc` | Scan all configured net/testpoint labels and return an ADC snapshot |
| `fixture.sample_net_adc_series` | Select a configured net/testpoint and return a bounded ADC time series |

## Resources

| Resource | Purpose |
| --- | --- |
| `fixture://status` | Current fixture status, selected channel and GPIO configuration |
| `fixture://net-map` | Configured net/testpoint labels mapped to MUX channels |
| `fixture://digital-inputs` | Configured digital input labels mapped to ESP32 GPIOs |

The ESP32 firmware is intentionally narrow. The Python bench server should control programmable power supplies, oscilloscopes, model calls, topology reasoning and high-risk diagnostic decisions.

## Build

Install ESP-IDF 5.4 or newer, then:

```bash
cd firmware/esp32-fixture
python3 tools/deploy.py build
```

The helper script configures the ESP32 target if needed and then runs
`idf.py build`. You can also run `idf.py set-target esp32 && idf.py build`
directly.

Verified locally with ESP-IDF v5.4.4 on ESP32 target. The project uses the
included `partitions.csv` for 4 MB ESP32 modules, giving the factory app a 3 MB
partition. The current app image is about 0xe7500 bytes, leaving about 70% free
in that app partition.

To flash:

```bash
python3 tools/deploy.py flash-monitor --port /dev/tty.usbserial-XXXX
```

To create a distributable flash bundle:

```bash
python3 tools/deploy.py bundle --zip
python3 tools/deploy.py verify-bundle
```

The bundle is written to `dist/esp32-fixture/` and includes `flash_args`,
`flasher_args.json`, `manifest.json`, SHA-256 hashes and a generated esptool
command. The verification step checks the manifest, hashes, `flash_args`,
`flasher_args.json`, MCP config metadata and app image size against the factory
partition. From inside the bundle directory:

```bash
python -m esptool --chip esp32 -b 460800 --before default_reset --after hard_reset write_flash @flash_args
```

For first bring-up, use `provision` so the same command can build, flash and
optionally run the non-destructive MCP smoke test:

```bash
python3 tools/deploy.py provision --port /dev/tty.usbserial-XXXX --smoke --prompt
```

After flashing, connect this computer to the ESP32 SoftAP when prompted. The
script then runs the MCP lifecycle handshake, `fixture.self_test` and the status
checks against `http://192.168.4.1/mcp`.

To list candidate serial ports:

```bash
python3 tools/deploy.py ports
```

To check the ESP-IDF environment, build outputs and serial ports:

```bash
python3 tools/deploy.py doctor
```

## Configure

Run:

```bash
idf.py menuconfig
```

Relevant options are under `AI Hardware Fixture`:

- Wi-Fi mode: SoftAP or Station.
- MCP HTTP port and endpoint name.
- DUT reset GPIO and active level.
- Load switch GPIO and active level.
- MUX select GPIOs, max channel and settle delay.
- Net/testpoint labels mapped to MUX channels.
- Digital input labels, GPIOs, active polarity and optional pulls.
- ADC unit/channel, millivolt calibration, default Vref and measurement scaling.
- ADC series limits: max points, per-point averaging, interval and total wait time.

ADC startup and reads are guarded: an invalid ADC channel or read failure will
be reported through status JSON and `fixture.read_adc_raw` instead of rebooting
the firmware.

Digital inputs are disabled by default because real fixture wiring varies. Set
`FIXTURE_DIN*_GPIO` to a valid GPIO and rename `FIXTURE_DIN*_LABEL` to board
signals such as `PGOOD`, `FAULT`, `IRQ` or `BOOT_MODE`. `fixture.self_test`
fails closed if a digital input uses an invalid GPIO, reuses an output GPIO, or
enables pull-up and pull-down at the same time.

When ADC calibration is available, ADC tools return both raw fields and
millivolt fields:

- Raw fields: `raw_avg`, `raw_min`, `raw_max`, `raw_last`.
- Calibration flag: `millivolts_valid`.
- ADC pin millivolt fields, present when valid: `mv_avg`, `mv_min`, `mv_max`, `mv_last`.
- Scaled fixture/net millivolt fields, present when valid: `scaled_mv_avg`,
  `scaled_mv_min`, `scaled_mv_max`, `scaled_mv_last`.

If calibration is unavailable, the tools still return raw ADC values and report
calibration status through `fixture.get_status` and `fixture.self_test`.

`fixture.scan_net_adc` returns a snapshot of every enabled net/testpoint in the
configured map. `fixture.sample_net_adc_series` returns a bounded `readings`
array for one net. Each item contains timing, raw fields, and calibrated/scaled
millivolt fields when available. These tools are intended for short
fixture-side observations that the Python bench server can turn into signal
features for model-assisted diagnosis.

For divider or buffer circuits, configure:

- `FIXTURE_ADC_SCALE_NUMERATOR`
- `FIXTURE_ADC_SCALE_DENOMINATOR`
- `FIXTURE_ADC_OFFSET_MV`

The conversion is `scaled_mv = mv * numerator / denominator + offset`.

For Station mode, configure:

- `FIXTURE_WIFI_STA_SSID`
- `FIXTURE_WIFI_STA_PASSWORD`

## Smoke Test

After flashing, connect your computer to the ESP32 SoftAP and run:

```bash
python3 tools/deploy.py smoke
```

The smoke test performs the MCP lifecycle handshake, lists tools, calls
`fixture.ping`, calls `fixture.get_status`, runs `fixture.self_test`, reads
`fixture://status`, reads `fixture://net-map`, reads `fixture://digital-inputs`,
scans configured digital inputs and, when ADC support is enabled, performs one
raw ADC sample. It does not toggle MUX lines, reset the DUT or enable the load
switch.

After the fixture wiring is confirmed, add `--exercise-net-adc-tool` to also
call `fixture.read_net_adc_raw`, `fixture.scan_net_adc` and
`fixture.sample_net_adc_series`. That action changes the MUX selection.

For Station mode or a custom endpoint:

```bash
python3 tools/deploy.py smoke --host 192.168.1.123 --http-port 80 --endpoint mcp
```

If you disable ADC support in `menuconfig`, run the smoke test with
`--skip-adc-tool`.

`fixture.self_test` fails closed when the configured MUX max channel cannot be
represented by the enabled select GPIOs, when an output GPIO is invalid for the
target, when any enabled net/testpoint mapping points at an unreachable channel,
or when ADC support is enabled but ADC initialization failed.

## Test With Curl

MCP clients should initialize the session before calling tools. This minimal
curl flow uses the baseline protocol version so `notifications/initialized` is
not required:

Initialize and capture the session id:

```bash
curl -i -X POST http://192.168.4.1/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "MCP-Protocol-Version: 2024-11-05" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl","version":"0.1"}}}'
```

Use the returned `MCP-Session-Id` header in subsequent requests.

List tools:

```bash
curl -X POST http://192.168.4.1/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "MCP-Protocol-Version: 2024-11-05" \
  -H "MCP-Session-Id: <session-id>" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

Ping:

```bash
curl -X POST http://192.168.4.1/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "MCP-Protocol-Version: 2024-11-05" \
  -H "MCP-Session-Id: <session-id>" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"fixture.ping","arguments":{}}}'
```

Set MUX channel:

```bash
curl -X POST http://192.168.4.1/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "MCP-Protocol-Version: 2024-11-05" \
  -H "MCP-Session-Id: <session-id>" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"fixture.set_mux_channel","arguments":{"channel":2}}}'
```

Select net/testpoint label:

```bash
curl -X POST http://192.168.4.1/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "MCP-Protocol-Version: 2024-11-05" \
  -H "MCP-Session-Id: <session-id>" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"fixture.select_net","arguments":{"net":"TP0"}}}'
```

Scan digital inputs:

```bash
curl -X POST http://192.168.4.1/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "MCP-Protocol-Version: 2024-11-05" \
  -H "MCP-Session-Id: <session-id>" \
  -d '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"fixture.scan_digital_inputs","arguments":{}}}'
```

Read one digital input:

```bash
curl -X POST http://192.168.4.1/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "MCP-Protocol-Version: 2024-11-05" \
  -H "MCP-Session-Id: <session-id>" \
  -d '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"fixture.read_digital_input","arguments":{"label":"PGOOD"}}}'
```

Read net/testpoint ADC:

```bash
curl -X POST http://192.168.4.1/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "MCP-Protocol-Version: 2024-11-05" \
  -H "MCP-Session-Id: <session-id>" \
  -d '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"fixture.read_net_adc_raw","arguments":{"net":"TP0","samples":8,"settle_ms":10}}}'
```

Scan all configured net/testpoint ADC values:

```bash
curl -X POST http://192.168.4.1/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "MCP-Protocol-Version: 2024-11-05" \
  -H "MCP-Session-Id: <session-id>" \
  -d '{"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"fixture.scan_net_adc","arguments":{"samples":4,"settle_ms":10}}}'
```

Sample a short net/testpoint ADC series:

```bash
curl -X POST http://192.168.4.1/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "MCP-Protocol-Version: 2024-11-05" \
  -H "MCP-Session-Id: <session-id>" \
  -d '{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"fixture.sample_net_adc_series","arguments":{"net":"TP0","points":4,"samples_per_point":4,"interval_ms":20,"settle_ms":10}}}'
```

Pulse reset:

```bash
curl -X POST http://192.168.4.1/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "MCP-Protocol-Version: 2024-11-05" \
  -H "MCP-Session-Id: <session-id>" \
  -d '{"jsonrpc":"2.0","id":10,"method":"tools/call","params":{"name":"fixture.reset_dut","arguments":{"pulse_ms":100}}}'
```

Read status resource:

```bash
curl -X POST http://192.168.4.1/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "MCP-Protocol-Version: 2024-11-05" \
  -H "MCP-Session-Id: <session-id>" \
  -d '{"jsonrpc":"2.0","id":11,"method":"resources/read","params":{"uri":"fixture://status"}}'
```

Read net map resource:

```bash
curl -X POST http://192.168.4.1/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "MCP-Protocol-Version: 2024-11-05" \
  -H "MCP-Session-Id: <session-id>" \
  -d '{"jsonrpc":"2.0","id":12,"method":"resources/read","params":{"uri":"fixture://net-map"}}'
```

Read digital input map resource:

```bash
curl -X POST http://192.168.4.1/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "MCP-Protocol-Version: 2024-11-05" \
  -H "MCP-Session-Id: <session-id>" \
  -d '{"jsonrpc":"2.0","id":13,"method":"resources/read","params":{"uri":"fixture://digital-inputs"}}'
```

## Default Pin Map

| Signal | Default GPIO |
| --- | --- |
| DUT reset | GPIO25 |
| Load switch | GPIO26 |
| MUX SEL0 | GPIO27 |
| MUX SEL1 | GPIO32 |
| MUX SEL2 | GPIO33 |
| ADC | ADC1 channel 0, classic ESP32 GPIO36 |
| Digital inputs | Disabled until configured |

Change these before connecting to real hardware.

## Default Net Map

By default, `TP0` through `TP7` map to MUX channels 0 through 7. Before using a
real board, change the labels in `idf.py menuconfig` to match your fixture
wiring, for example `VIN`, `3V3`, `MCU_NRST`, `I2C_SCL` or actual testpoint
names from the board context.

## Safety Notes

- Keep reset and load-switch wiring fail-safe with pull resistors.
- Use external driver transistors/isolators for relays, loads and anything beyond GPIO current.
- Do not connect the MUX output directly to high-energy nodes.
- Avoid ESP32 boot strapping pins GPIO0, GPIO2, GPIO4, GPIO5, GPIO12 and GPIO15 for fixture outputs unless the hardware guarantees safe boot levels.
- Treat `fixture.set_load_switch` as a high-risk action in the Python bench policy layer.
- The ESP32 MCP server is a fixture actuator, not the diagnostic authority.
