# check_isg_wpm_sensor

A Nagios/Icinga plugin for Stiebel Eltron heat pumps with a heat pump manager
WPM (WPMsystem, WPM 3, WPM 3i), read via the Modbus TCP interface of the
Stiebel Eltron Internet Service Gateway (ISG web with the *Modbus TCP/IP*
software extension). It is the sibling of
[check_esphome_sensor](https://github.com/Aikhjarto/check_esphome_sensor),
[check_tasmota_sensor](https://github.com/Aikhjarto/check_tasmota_sensor) and
[check_homeassistant_sensor](https://github.com/Aikhjarto/check_homeassistant_sensor),
with the same kinds of checks and options. Python 3.8 or newer, standard
library only.

The plugin only reads; it never writes a register of the heat pump.

## Features

- **Five kinds of checks** (`--check`)
  - `sensor`: compare a numeric value against `-w`/`-c` thresholds. Unlike
    most plugins, `-c` may be smaller than `-w`: whichever of the two is
    larger decides whether higher or lower values count as worse (e.g. a
    heating pressure where *lower* is bad).
  - `compare`: check that `--entity` stays above (or below) the
    `--compare-entity` by a margin, see [below](#comparing-two-values).
  - `binary`: evaluate a boolean expression (`and`/`or`/`not`/parentheses)
    over on/off values such as pumps and status bits, e.g.
    `--binary-expr "operating_status.compressor and not evu_release"`.
  - `text`: match a value, or the name of a coded value such as the
    operating mode, against a Python regular expression (`--regex`).
  - `fault`: CRITICAL if the heat pump reports a fault (with the active error
    number) or its CAN bus an error.
- **`--list`**: instead of `--check`, print all values the controller
  provides, with their register and the name from the Stiebel Eltron
  documentation, so you know what to pass to `--entity`. `--all` also lists
  the values the controller does not provide.

## Usage

```console
$ check_isg_wpm_sensor --help
```

| Option | Meaning |
|---|---|
| `-H`, `--host` | Host name or address of the ISG (required) |
| `-p`, `--port` | Modbus TCP port (default: 502) |
| `--unit-id` | Modbus unit id (default: 1, which the ISG does not allow to change) |
| `-t`, `--timeout` | Timeout in seconds (default: 10) |
| `--entity` | Name of the value (see `--list`), or its register number |
| `-w`, `-c` | Thresholds; for `compare`, the smallest acceptable margins |
| `--unavailable-state` | State if the controller does not provide a value (default: unknown) |

### Examples

```console
# Outside temperature
check_isg_wpm_sensor -H servicewelt.local --check sensor --entity outside_temperature -w 35 -c 40

# Lower is worse: -c below -w
check_isg_wpm_sensor -H servicewelt.local --check sensor --entity heating_pressure -w 1.2 -c 1.0

# The same value by its register number
check_isg_wpm_sensor -H servicewelt.local --check sensor --entity 520 -w 1.2 -c 1.0

# A room has to stay at least 5 K above its dew point
check_isg_wpm_sensor -H servicewelt.local --check compare --entity actual_room_temperature_hk_2 \
    --compare-entity room_dew_point_temperature_hk_2 --must-be above -w 5 -c 2

# Hot water may fall at most 10 K below its set temperature
check_isg_wpm_sensor -H servicewelt.local --check compare --entity actual_temperature_dhw \
    --compare-entity set_temperature_dhw --must-be above -w -5 -c -10

# Fault output of the controller
check_isg_wpm_sensor -H servicewelt.local --check binary --binary-expr 'fault_output'

# Operating mode
check_isg_wpm_sensor -H servicewelt.local --check text --entity operating_mode \
    --regex '^(program|comfort|eco)$'

# Faults and bus errors
check_isg_wpm_sensor -H servicewelt.local --check fault

# All values the controller provides
check_isg_wpm_sensor -H servicewelt.local --list
```

```
OK: outside_temperature is 17.5 °C | 'outside_temperature'=17.5;35;40
OK: actual_room_temperature_hk_2 is 12.3 K above room_dew_point_temperature_hk_2 (19.4 °C vs 7.1 °C) | 'margin'=12.3;5;2 'actual_room_temperature_hk_2'=19.4 'room_dew_point_temperature_hk_2'=7.1
OK: no fault, bus ok | 'fault'=0;;1 'bus_status'=0
```

### Values

The names follow the Home Assistant integration
[stiebel_eltron_isg_component](https://github.com/pail23/stiebel_eltron_isg_component)
where they exist. `--list` shows each value with its register and the object
name of the Stiebel Eltron documentation *Modbus TCP/IP*, section 6
(*Modbus-Systemwerte für Wärmepumpen mit WPM*):

- **system values** (registers 501-608): temperatures, pressures and flow
  rates, e.g. `outside_temperature`, `actual_flow_temperature`,
  `actual_temperature_dhw`, `heating_pressure`; per heat pump with the
  suffix `_hp_1` to `_hp_6`, per heating circuit with `_hk_1` to `_hk_5`
- **system parameters** (registers 1501-1521): e.g. `operating_mode`,
  `comfort_temperature_hk_1`, `heating_curve_rise_hk_1`
- **system status** (registers 2501-2547): the bit coded
  `operating_status.*` (e.g. `operating_status.compressor`,
  `operating_status.defrost`) and `operating_status_wpm3.*`, `evu_release`,
  `fault_status`, `bus_status`, `active_error`, the pumps and mixers
- **energy data** (registers 3501-3655): heat and consumed energy per day and
  in total, and runtimes. The ISG splits each total into a kWh and a MWh
  register; the plugin adds them up to one value in kWh, e.g.
  `vd_heating_total`.

Coded values are shown by name: `operating_mode` is `emergency`, `standby`,
`program`, `comfort`, `eco` or `dhw` (Notbetrieb, Bereitschaftsbetrieb,
Programmbetrieb, Komfortbetrieb, ECO-Betrieb, Warmwasserbetrieb), and
`bus_status` is `ok`, `error`, `error passive`, `bus off` or
`physical error`. `--check sensor` compares their number, `--check text`
their name.

### Registers

Register numbers are the ones of the documentation, which counts from 1: the
outside temperature, register 507, is read at protocol address 506 with
function code 4 (*read input registers*); the system parameters are holding
registers, read with function code 3. `--entity` also takes a register
number, e.g. `--entity 507`.

A value the controller does not provide reads as 0x8000; the check then
reports `--unavailable-state`, by default UNKNOWN. Signed values from 0x8000
to 0x9FFF are treated as codes rather than values, as they would be below
-2457 °C: the ISG reports e.g. 0x8004 for a missing cooling fan
temperature, and 0x9000 for a function that is switched off, such as the
fixed value operation.

### Comparing two values

`--check compare` checks that one value stays on one side of another:
`--must-be above` requires `--entity` to be larger than `--compare-entity`,
`--must-be below` smaller. `-w` and `-c` are the smallest acceptable margins
between the two: a margin at or below `-w` is WARNING, at or below `-c`
CRITICAL. So `-w` must not be smaller than `-c`, and a value on the wrong
side is CRITICAL whenever `-c` is 0 or more. Negative margins allow the
value to be on the wrong side by that much, as in the hot water example
above. The margin between two temperatures is shown in K. Values with
different units are UNKNOWN.

## Output and exit codes

Every check prints one line: the state, a summary and the performance data.
Units are shown in the text; in the performance data only those the
[monitoring plugins guidelines](https://www.monitoring-plugins.org/doc/guidelines.html#AEN201)
allow (`%` here), so °C, bar or kWh are left out there.

An ISG that cannot be reached is CRITICAL. An unknown value, a value the
controller does not provide and a wrong command line are UNKNOWN.

## Installation

```console
$ pip install .
```

This installs a `check_isg_wpm_sensor` console script.

## Tests

```console
$ PYTHONPATH=src python3 -m unittest -v
```

The tests run the plugin against a fake ISG Modbus TCP server with values
recorded from a WPMsystem.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
