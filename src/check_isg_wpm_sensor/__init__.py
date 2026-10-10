# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Thomas Wagner <wagner-thomas@gmx.at>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
"""check_isg_wpm_sensor - Nagios/Icinga plugin for Stiebel Eltron heat pumps with WPM.

Reads the Modbus TCP interface of a Stiebel Eltron Internet Service Gateway
(ISG web with the Modbus TCP/IP software extension) connected to a heat pump
manager WPM, WPM 3 or WPM 3i. The plugin only reads; it never writes a
register.

Five kinds of checks (--check):
  sensor  Compare a numeric value against -w/-c thresholds. Unlike most
          plugins, -c may be smaller than -w: whichever of the two is
          larger decides whether higher or lower values are considered
          worse.
  compare Check that --entity stays above (or below, --must-be) the
          --compare-entity by a margin: -w and -c are the smallest
          acceptable margins, e.g. a flow temperature at least 5 K above
          the return temperature.
  binary  Evaluate a boolean expression (and/or/not/parentheses) over
          on/off values such as pumps and status bits, e.g.
          --binary-expr "operating_status.compressor and not evu_release".
  text    Match a value, or the name of a coded value such as the
          operating mode, against a Python regular expression (--regex).
  fault   CRITICAL if the heat pump reports a fault or its CAN bus an error.

Pass --list instead of --check to print all values the controller provides,
with their register, so you know what to pass to --entity.

Register numbers are the ones of the Stiebel Eltron documentation ("Modbus
TCP/IP", section 6), which counts from 1; on the wire they are sent minus 1.
"""
import argparse
import ast
import random
import re
import socket
import struct
import sys

__version__ = "0.2.1"

STATE_OK = 0
STATE_WARNING = 1
STATE_CRITICAL = 2
STATE_UNKNOWN = 3
STATE_NAMES = {
    STATE_OK: "OK",
    STATE_WARNING: "WARNING",
    STATE_CRITICAL: "CRITICAL",
    STATE_UNKNOWN: "UNKNOWN",
}
STATE_BY_NAME = {"ok": STATE_OK, "warning": STATE_WARNING, "critical": STATE_CRITICAL, "unknown": STATE_UNKNOWN}

# units of measurement the monitoring plugins guidelines allow in perfdata
PERFDATA_UOMS = ("s", "ms", "us", "%", "B", "KB", "MB", "GB", "TB", "c")

# the ISG answers objects the controller does not have with 0x8000
UNAVAILABLE = 0x8000
# a switched off function, e.g. fixed value operation, reads 0x9000
SWITCHED_OFF = 0x9000
# signed values from 0x8000 up to here are codes, not values: they would be
# below -2457 degrees (data type 2) or -245 bar (data type 7); the ISG uses
# e.g. 0x8004 for a missing cooling fan temperature
SIGNED_CODES_END = 0xA000

MODBUS_EXCEPTIONS = {
    1: "illegal function",
    2: "illegal data address",
    3: "illegal data value",
    4: "server device failure",
    5: "acknowledge",
    6: "server device busy",
    10: "gateway path unavailable",
    11: "gateway target device failed to respond",
}

# largest number of registers one read may request
MAX_READ = 120


def die(state, message):
    print(f"{STATE_NAMES[state]}: {message}")
    sys.exit(state)


# ---------------------------------------------------------------------------
# register map, after "Modbus TCP/IP", section 6: heat pumps with WPM
# ---------------------------------------------------------------------------

# data types of the documentation: (signed, factor)
DATA_TYPES = {
    2: (True, 0.1),
    6: (False, 1),
    7: (True, 0.01),
    8: (False, 1),
}


class Entity:
    """A value of the controller, read from one or two registers.

    kind is "number", "boolean" (0/1), "bit" (one bit of a bit coded
    register), "coded" (an integer with named values) or "total" (an energy
    counter split into a kWh register and a MWh register).
    """

    def __init__(self, name, register, space, dtype=6, unit="", label="", kind="number", coding=None,
                 bit=None, mwh_register=None):
        self.name = name
        self.register = register
        self.space = space
        self.dtype = dtype
        self.unit = unit
        self.label = label
        self.kind = kind
        self.coding = coding or {}
        self.bit = bit
        self.mwh_register = mwh_register

    @property
    def registers(self):
        return [self.register] if self.mwh_register is None else [self.register, self.mwh_register]


class Value:
    """A decoded value: number is None if the controller does not provide it."""

    def __init__(self, entity, number=None, text=None):
        self.entity = entity
        self.number = number
        self.text = text

    @property
    def available(self):
        return self.number is not None

    def display(self):
        if not self.available:
            return self.text or "not available"
        if self.entity.kind in ("boolean", "bit"):
            return "ON" if self.number else "OFF"
        if self.text:
            return f"{self.text} ({format_value(self.number)})"
        unit = self.entity.unit
        return format_value(self.number) + (f" {unit}" if unit and unit not in ("%",) else unit)


def _build_entities():
    entities = []

    def add(*args, **kwargs):
        entities.append(Entity(*args, **kwargs))

    # block 1: system values (input registers)
    for register, name, dtype, unit, label in (
        (501, "actual_temperature_fe7", 2, "°C", "ISTTEMPERATUR FE7"),
        (502, "set_temperature_fe7", 2, "°C", "SOLLTEMPERATUR FE7"),
        (503, "actual_temperature_fek", 2, "°C", "ISTTEMPERATUR FEK"),
        (504, "set_temperature_fek", 2, "°C", "SOLLTEMPERATUR FEK"),
        (505, "relative_humidity", 2, "%", "RAUMFEUCHTE"),
        (506, "dew_point_temperature", 2, "°C", "TAUPUNKTTEMPERATUR"),
        (507, "outside_temperature", 2, "°C", "AUSSENTEMPERATUR"),
        (508, "actual_temperature_hk_1", 2, "°C", "ISTTEMPERATUR HK 1"),
        (509, "set_temperature_hk_1_wpm3i", 2, "°C", "SOLLTEMPERATUR HK 1 (WPM 3i)"),
        (510, "set_temperature_hk_1", 2, "°C", "SOLLTEMPERATUR HK 1"),
        (511, "actual_temperature_hk_2", 2, "°C", "ISTTEMPERATUR HK 2"),
        (512, "set_temperature_hk_2", 2, "°C", "SOLLTEMPERATUR HK 2"),
        (513, "actual_flow_temperature_wp", 2, "°C", "VORLAUFISTTEMPERATUR WP"),
        (514, "actual_flow_temperature_nhz", 2, "°C", "VORLAUFISTTEMPERATUR NHZ"),
        (515, "actual_flow_temperature", 2, "°C", "VORLAUFISTTEMPERATUR"),
        (516, "actual_return_temperature", 2, "°C", "RUECKLAUFISTTEMPERATUR"),
        (517, "set_fixed_temperature", 2, "°C", "FESTWERTSOLLTEMPERATUR"),
        (518, "actual_buffer_temperature", 2, "°C", "PUFFERISTTEMPERATUR"),
        (519, "set_buffer_temperature", 2, "°C", "PUFFERSOLLTEMPERATUR"),
        (520, "heating_pressure", 7, "bar", "HEIZUNGSDRUCK"),
        (521, "flow_rate", 2, "l/min", "VOLUMENSTROM"),
        (522, "actual_temperature_dhw", 2, "°C", "ISTTEMPERATUR Warmwasser"),
        (523, "set_temperature_dhw", 2, "°C", "SOLLTEMPERATUR Warmwasser"),
        (524, "actual_temperature_fan", 2, "K", "ISTTEMPERATUR GEBLAESE Kühlen"),
        (525, "set_temperature_fan", 2, "K", "SOLLTEMPERATUR GEBLAESE Kühlen"),
        (526, "actual_temperature_area", 2, "K", "ISTTEMPERATUR FLAECHE Kühlen"),
        (527, "set_temperature_area", 2, "K", "SOLLTEMPERATUR FLAECHE Kühlen"),
        (528, "collector_temperature", 2, "°C", "KOLLEKTORTEMPERATUR Solar"),
        (529, "cylinder_temperature", 2, "°C", "SPEICHERTEMPERATUR Solar"),
        (530, "runtime_solar", 6, "h", "LAUFZEIT Solar"),
        (531, "actual_temperature_external", 2, "°C", "ISTTEMPERATUR Wärmeerzeuger extern"),
        (532, "set_temperature_external", 2, "K", "SOLLTEMPERATUR Wärmeerzeuger extern"),
        (533, "application_limit_hzg", 2, "°C", "EINSATZGRENZE HZG"),
        (534, "application_limit_ww", 2, "°C", "EINSATZGRENZE WW"),
        (535, "runtime_external", 6, "h", "LAUFZEIT Wärmeerzeuger extern"),
        (536, "source_temperature", 2, "°C", "QUELLENTEMPERATUR"),
        (537, "min_source_temperature", 2, "°C", "QUELLENTEMPERATUR MIN"),
        (538, "source_pressure", 7, "bar", "QUELLENDRUCK"),
        (539, "hot_gas_temperature", 2, "°C", "HEISSGASTEMPERATUR"),
        (540, "high_pressure", 2, "bar", "DRUCK HOCHDRUCK"),
        (541, "low_pressure", 2, "bar", "DRUCK NIEDERDRUCK"),
    ):
        add(name, register, "input", dtype, unit, label)
    for hp in range(1, 7):
        base = 542 + 7 * (hp - 1)
        for offset, name, dtype, unit, label in (
            (0, "return_temperature", 2, "°C", "RUECKLAUFTEMPERATUR"),
            (1, "flow_temperature", 2, "°C", "VORLAUFTEMPERATUR"),
            (2, "hot_gas_temperature", 2, "°C", "HEISSGASTEMPERATUR"),
            (3, "low_pressure", 7, "bar", "DRUCK NIEDERDRUCK"),
            (4, "mean_pressure", 7, "bar", "DRUCK MITTELDRUCK"),
            (5, "high_pressure", 7, "bar", "DRUCK HOCHDRUCK"),
            (6, "water_flow_rate", 2, "l/min", "WP WASSERVOLUMENSTROM"),
        ):
            add(f"{name}_hp_{hp}", base + offset, "input", dtype, unit, f"{label} Wärmepumpe {hp}")
    for hk in range(1, 6):
        base = 584 + 4 * (hk - 1)
        for offset, name, unit, label in (
            (0, "actual_room_temperature", "°C", "ISTTEMPERATUR Raumtemperatur"),
            (1, "set_room_temperature", "°C", "SOLLTEMPERATUR Raumtemperatur"),
            (2, "room_humidity", "%", "RAUMFEUCHTE"),
            (3, "room_dew_point_temperature", "°C", "TAUPUNKTTEMPERATUR"),
        ):
            add(f"{name}_hk_{hk}", base + offset, "input", 2, unit, f"{label} Heizkreis {hk}")
    for kk in range(1, 6):
        add(f"set_room_temperature_cooling_{kk}", 603 + kk, "input", 2, "°C",
            f"SOLLTEMPERATUR Raumtemperatur Kühlkreis {kk}")

    # block 2: system parameters (holding registers, read only here)
    add("operating_mode", 1501, "holding", 8, label="BETRIEBSART", kind="coded",
        coding={0: "emergency", 1: "standby", 2: "program", 3: "comfort", 4: "eco", 5: "dhw"})
    for register, name, dtype, unit, label in (
        (1502, "comfort_temperature_hk_1", 2, "°C", "KOMFORT TEMPERATUR Heizkreis 1"),
        (1503, "eco_temperature_hk_1", 2, "°C", "ECO TEMPERATUR Heizkreis 1"),
        (1504, "heating_curve_rise_hk_1", 7, "", "STEIGUNG HEIZKURVE Heizkreis 1"),
        (1505, "comfort_temperature_hk_2", 2, "°C", "KOMFORT TEMPERATUR Heizkreis 2"),
        (1506, "eco_temperature_hk_2", 2, "°C", "ECO TEMPERATUR Heizkreis 2"),
        (1507, "heating_curve_rise_hk_2", 7, "", "STEIGUNG HEIZKURVE Heizkreis 2"),
        (1508, "fixed_value_operation", 2, "°C", "FESTWERTBETRIEB"),
        (1509, "dual_mode_temperature_hzg", 2, "°C", "BIVALENZTEMPERATUR HZG"),
        (1510, "comfort_temperature_dhw", 2, "°C", "KOMFORT TEMPERATUR Warmwasser"),
        (1511, "eco_temperature_dhw", 2, "°C", "ECO TEMPERATUR Warmwasser"),
        (1512, "dhw_stages", 8, "", "WARMWASSERSTUFEN"),
        (1513, "dual_mode_temperature_ww", 2, "°C", "BIVALENZTEMPERATUR WW"),
        (1514, "set_flow_temperature_area", 2, "°C", "VORLAUFSOLLTEMPERATUR Flächenkühlung"),
        (1515, "flow_temperature_hysteresis_area", 2, "K", "HYSTERESE VORLAUFTEMP Flächenkühlung"),
        (1516, "set_room_temperature_area", 2, "°C", "RAUMSOLLTEMPERATUR Flächenkühlung"),
        (1517, "set_flow_temperature_fan", 2, "°C", "VORLAUFSOLLTEMPERATUR Gebläsekühlung"),
        (1518, "flow_temperature_hysteresis_fan", 2, "K", "HYSTERESE VORLAUFTEMP Gebläsekühlung"),
        (1519, "set_room_temperature_fan", 2, "°C", "RAUMSOLLTEMPERATUR Gebläsekühlung"),
    ):
        add(name, register, "holding", dtype, unit, label)
    add("reset", 1520, "holding", 6, label="RESET", kind="coded",
        coding={0: "off", 1: "system", 2: "error list", 3: "heat pump"})
    add("restart_isg", 1521, "holding", 6, label="RESTART-ISG", kind="coded",
        coding={0: "off", 1: "restart", 2: "service button"})

    # block 3: system status (input registers)
    add("operating_status", 2501, "input", 6, label="BETRIEBSSTATUS (bit coded)")
    for bit, name, label in (
        (0, "hk_1_pump", "HK 1 PUMPE"),
        (1, "hk_2_pump", "HK 2 PUMPE"),
        (2, "heat_up_program", "AUFHEIZPROGRAMM"),
        (3, "nhz_stages", "NHZ STUFEN IN BETRIEB"),
        (4, "heating", "WP IM HEIZBETRIEB"),
        (5, "dhw", "WP IM WARMWASSERBETRIEB"),
        (6, "compressor", "VERDICHTER IN BETRIEB"),
        (7, "summer_mode", "SOMMERBETRIEB AKTIV"),
        (8, "cooling", "KUEHLBETRIEB AKTIV"),
        (9, "defrost", "MIN. EINE IWS IM ABTAUBETRIEB"),
        (10, "silent_mode_1", "SILENTMODE 1 AKTIV"),
        (11, "silent_mode_2", "SILENTMODE 2 AKTIV (WP AUS)"),
    ):
        add(f"operating_status.{name}", 2501, "input", 6, label=f"BETRIEBSSTATUS B{bit} {label}", kind="bit", bit=bit)
    add("evu_release", 2502, "input", 8, label="EVU-FREIGABE B0", kind="bit", bit=0)
    add("operating_status_wpm3", 2503, "input", 6, label="BETRIEBSSTATUS WPM 3 (bit coded)")
    for bit in range(14):
        if bit < 6:
            name, label = f"compressor_{bit + 1}", f"VERDICHTER-{bit + 1}"
        elif bit < 12:
            name, label = f"buffer_charging_pump_{bit - 5}", f"PUFFERLADEPUMPE-{bit - 5}"
        else:
            name, label = f"nhz_{bit - 11}", f"NHZ-{bit - 11}"
        add(f"operating_status_wpm3.{name}", 2503, "input", 6, label=f"BETRIEBSSTATUS B{bit} {label}", kind="bit",
            bit=bit)
    add("fault_status", 2504, "input", 6, label="FEHLERSTATUS", kind="boolean")
    add("bus_status", 2505, "input", 6, label="BUS-STATUS", kind="coded",
        coding={0: "ok", -1: "error", -2: "error passive", -3: "bus off", -4: "physical error"})
    add("defrost_initiated", 2506, "input", 6, label="ABTAUEN EINGELEITET", kind="boolean")
    add("active_error", 2507, "input", 6, label="aktiver Fehler")
    add("message_number", 2508, "input", 6, label="MELDUNGSNUMMER")
    for register, name, label in (
        (2509, "heating_circuit_pump_1", "HEIZKREISPUMPE 1"),
        (2510, "heating_circuit_pump_2", "HEIZKREISPUMPE 2"),
        (2511, "heating_circuit_pump_3", "HEIZKREISPUMPE 3"),
        (2512, "buffer_charging_pump_1", "PUFFERLADEPUMPE 1"),
        (2513, "buffer_charging_pump_2", "PUFFERLADEPUMPE 2"),
        (2514, "dhw_charging_pump", "WARMWASSERLADEPUMPE"),
        (2515, "source_pump", "QUELLENPUMPE"),
        (2516, "fault_output", "STÖRAUSGANG"),
        (2517, "circulation_pump", "ZIRKULATIONSPUMPE"),
        (2518, "we_2_dhw", "2. WE WARMWASSER"),
        (2519, "we_2_heating", "2. WE HEIZUNG"),
        (2520, "cooling_mode", "KÜHLBETRIEB"),
        (2521, "mixer_open_hk_2", "MISCHER AUF HEIZKREIS 2"),
        (2522, "mixer_close_hk_2", "MISCHER ZU HEIZKREIS 2"),
        (2523, "mixer_open_hk_3", "MISCHER AUF HEIZKREIS 3"),
        (2524, "mixer_close_hk_3", "MISCHER ZU HEIZKREIS 3"),
        (2525, "nhz_1", "NHZ 1"),
        (2526, "nhz_2", "NHZ 2"),
        (2527, "nhz_1_2", "NHZ 1/2"),
        (2528, "heating_circuit_pump_4", "HEIZKREISPUMPE 4"),
        (2529, "heating_circuit_pump_5", "HEIZKREISPUMPE 5"),
        (2530, "buffer_charging_pump_3", "PUFFERLADEPUMPE 3"),
        (2531, "buffer_charging_pump_4", "PUFFERLADEPUMPE 4"),
        (2532, "buffer_charging_pump_5", "PUFFERLADEPUMPE 5"),
        (2533, "buffer_charging_pump_6", "PUFFERLADEPUMPE 6"),
        (2534, "diff_controller_pump_1", "PUMPE DIFFERENZREGLER 1"),
        (2535, "diff_controller_pump_2", "PUMPE DIFFERENZREGLER 2"),
        (2536, "pool_pump_primary", "SCHWIMMBADPUMPE PRIMÄR"),
        (2537, "pool_pump_secondary", "SCHWIMMBADPUMPE SEKUNDÄR"),
        (2538, "mixer_open_hk_4", "MISCHER AUF HEIZKREIS 4"),
        (2539, "mixer_close_hk_4", "MISCHER ZU HEIZKREIS 4"),
        (2540, "mixer_open_hk_5", "MISCHER AUF HEIZKREIS 5"),
        (2541, "mixer_close_hk_5", "MISCHER ZU HEIZKREIS 5"),
    ) + tuple((2541 + hp, f"compressor_{hp}", f"VERDICHTER {hp}") for hp in range(1, 7)):
        add(name, register, "input", 6, label=label, kind="boolean")

    # block 4: energy data (input registers); totals are a kWh and a MWh register
    def energy(prefix, suffix, base, per_heat_pump, label_suffix):
        registers = [("vd_heating_day", 1, "kWh", "VD HEIZEN TAG Wärmemenge"),
                     ("vd_heating_total", 2, "kWh", "VD HEIZEN SUMME Wärmemenge"),
                     ("vd_dhw_day", 1, "kWh", "VD WARMWASSER TAG Wärmemenge"),
                     ("vd_dhw_total", 2, "kWh", "VD WARMWASSER SUMME Wärmemenge")]
        if not per_heat_pump or suffix == "_hp_1":
            registers += [("nhz_heating_total", 2, "kWh", "NHZ HEIZEN SUMME Wärmemenge"),
                          ("nhz_dhw_total", 2, "kWh", "NHZ WARMWASSER SUMME Wärmemenge")]
        registers += [("vd_heating_day_consumed", 1, "kWh", "VD HEIZEN TAG Leistungsaufnahme"),
                      ("vd_heating_total_consumed", 2, "kWh", "VD HEIZEN SUMME Leistungsaufnahme"),
                      ("vd_dhw_day_consumed", 1, "kWh", "VD WARMWASSER TAG Leistungsaufnahme"),
                      ("vd_dhw_total_consumed", 2, "kWh", "VD WARMWASSER SUMME Leistungsaufnahme")]
        if per_heat_pump:
            registers += [("runtime_vd_1_heating", 1, "h", "VD 1 HEIZEN Laufzeit"),
                          ("runtime_vd_2_heating", 1, "h", "VD 2 HEIZEN Laufzeit"),
                          ("runtime_vd_1_2_heating", 1, "h", "VD 1/2 HEIZEN Laufzeit"),
                          ("runtime_vd_1_dhw", 1, "h", "VD 1 WARMWASSER Laufzeit"),
                          ("runtime_vd_2_dhw", 1, "h", "VD 2 WARMWASSER Laufzeit"),
                          ("runtime_vd_1_2_dhw", 1, "h", "VD 1/2 WARMWASSER Laufzeit"),
                          ("runtime_vd_cooling", 1, "h", "VD KUEHLEN Laufzeit")]
        else:
            registers += [("runtime_vd_heating", 1, "h", "VD HEIZEN Laufzeit"),
                          ("runtime_vd_dhw", 1, "h", "VD WARMWASSER Laufzeit"),
                          ("runtime_vd_cooling", 1, "h", "VD KUEHLEN Laufzeit"),
                          ("runtime_nhz_1", 1, "h", "NHZ 1 Laufzeit"),
                          ("runtime_nhz_2", 1, "h", "NHZ 2 Laufzeit"),
                          ("runtime_nhz_1_2", 1, "h", "NHZ 1/2 Laufzeit")]
        register = base
        for name, count, unit, label in registers:
            if count == 2:
                add(f"{prefix}{name}{suffix}", register, "input", 6, unit, f"{label}{label_suffix} (kWh + MWh)",
                    kind="total", mwh_register=register + 1)
            else:
                add(f"{prefix}{name}{suffix}", register, "input", 6, unit, f"{label}{label_suffix}")
            register += count
        return register

    energy("", "", 3501, False, " aller WP")
    register = energy("", "_hp_1", 3523, True, " WP 1")
    for register, name, label in ((register, "runtime_reheating_1", "NHZ 1 Laufzeit Nachheizstufe"),
                                  (register + 1, "runtime_reheating_2", "NHZ 2 Laufzeit Nachheizstufe"),
                                  (register + 2, "runtime_reheating_1_2", "NHZ 1/2 Laufzeit Nachheizstufe")):
        add(name, register, "input", 6, "h", label)
    register += 1
    for hp in range(2, 7):
        register = energy("", f"_hp_{hp}", register, True, f" WP {hp}")
    for hp in range(1, 7):
        add(f"runtime_heating_hp_{hp}", 3642 + 2 * hp, "input", 6, "h", f"VD HEIZEN Laufzeit WP {hp}")
        add(f"runtime_dhw_hp_{hp}", 3643 + 2 * hp, "input", 6, "h", f"VD WARMWASSER Laufzeit WP {hp}")

    # not in section 6; the controller model, as pystiebeleltron reads it
    add("controller_model", 5002, "input", 6, label="controller model", kind="coded",
        coding={103: "LWZ", 104: "LWZ x04 SOL", 390: "WPM 3", 391: "WPM 3i", 449: "WPMsystem", 551: "LWZ R290"})
    return entities


ENTITIES = _build_entities()
ENTITY_BY_NAME = {entity.name: entity for entity in ENTITIES}


def find_entity(name):
    """An entity by name, or by its register number for a single register value."""
    if name in ENTITY_BY_NAME:
        return ENTITY_BY_NAME[name]
    if name.isdigit():
        matches = [entity for entity in ENTITIES if entity.register == int(name) and entity.kind != "bit"]
        if matches:
            return matches[0]
    die(STATE_UNKNOWN, f"Unknown entity: {name} (see --list)")


# ---------------------------------------------------------------------------
# Modbus TCP
# ---------------------------------------------------------------------------

class ModbusClient:
    """A minimal Modbus TCP client for function codes 3 and 4."""

    def __init__(self, host, port, unit_id, timeout):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        self.sock = None

    def connect(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            die(STATE_CRITICAL, f"Cannot connect to the ISG at {self.host}:{self.port}: {exc.strerror or exc}")

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def _receive(self, count):
        data = b""
        while len(data) < count:
            try:
                chunk = self.sock.recv(count - len(data))
            except socket.timeout:
                die(STATE_CRITICAL, f"The ISG at {self.host}:{self.port} did not answer within {self.timeout}s")
            except OSError as exc:
                die(STATE_CRITICAL, f"Connection to the ISG at {self.host}:{self.port} failed: {exc.strerror or exc}")
            if not chunk:
                die(STATE_UNKNOWN, f"The ISG at {self.host}:{self.port} closed the connection")
            data += chunk
        return data

    def read(self, space, register, count):
        """Read count registers starting at the documented (1 based) register number.

        Returns the list of 16 bit words, or raises ModbusException.
        """
        if self.sock is None:
            self.connect()
        function = 4 if space == "input" else 3
        transaction = random.randint(0, 0xFFFF)
        request = struct.pack(">HHHBBHH", transaction, 0, 6, self.unit_id, function, register - 1, count)
        try:
            self.sock.sendall(request)
        except OSError as exc:
            die(STATE_CRITICAL, f"Connection to the ISG at {self.host}:{self.port} failed: {exc.strerror or exc}")
        header = self._receive(7)
        answer_transaction, protocol, length, _unit = struct.unpack(">HHHB", header)
        if answer_transaction != transaction or protocol != 0 or length < 2:
            die(STATE_UNKNOWN, "Unexpected Modbus answer from the ISG")
        body = self._receive(length - 1)
        if body[0] == function | 0x80:
            raise ModbusException(body[1])
        if body[0] != function or body[1] != 2 * count or len(body) != 2 + 2 * count:
            die(STATE_UNKNOWN, "Unexpected Modbus answer from the ISG")
        return list(struct.unpack(f">{count}H", body[2:]))


class ModbusException(Exception):
    def __init__(self, code):
        super().__init__(MODBUS_EXCEPTIONS.get(code, f"exception {code}"))
        self.code = code


def spans(registers):
    """Contiguous runs of register numbers, joined across small gaps, at most MAX_READ long."""
    result = []
    for register in sorted(set(registers)):
        if result and register - result[-1][0] < MAX_READ and register - result[-1][1] <= 8:
            result[-1][1] = register
        else:
            result.append([register, register])
    return result


def read_words(client, entities):
    """The raw words of the registers of entities, as {(space, register): word}."""
    words = {}
    for space in ("input", "holding"):
        wanted = [register for entity in entities if entity.space == space for register in entity.registers]
        for first, last in spans(wanted):
            try:
                values = client.read(space, first, last - first + 1)
            except ModbusException as exc:
                if last > first:
                    # a register in the gap may be refused: read the wanted ones on their own
                    for register in sorted(set(wanted)):
                        if first <= register <= last:
                            words[(space, register)] = read_single(client, space, register)
                    continue
                words[(space, first)] = exc
                continue
            for offset, word in enumerate(values):
                words[(space, first + offset)] = word
    return words


def read_single(client, space, register):
    try:
        return client.read(space, register, 1)[0]
    except ModbusException as exc:
        return exc


def decode(entity, words):
    """A Value from the raw words; a refused register reads as not available."""
    raw = [words.get((entity.space, register)) for register in entity.registers]
    for word in raw:
        if isinstance(word, ModbusException):
            return Value(entity, text=f"not served ({word})")
    if any(word is None for word in raw):
        return Value(entity, text="not read")
    if entity.kind == "total":
        if UNAVAILABLE in raw:
            return Value(entity)
        return Value(entity, raw[0] + 1000 * raw[1])
    word = raw[0]
    if word == UNAVAILABLE:
        return Value(entity)
    if entity.kind == "bit":
        return Value(entity, (word >> entity.bit) & 1)
    signed, factor = DATA_TYPES[entity.dtype]
    if signed and entity.kind == "number" and UNAVAILABLE <= word < SIGNED_CODES_END:
        return Value(entity, text="off" if word == SWITCHED_OFF else f"not available (0x{word:04X})")
    if entity.kind == "coded" and entity.coding and min(entity.coding) < 0:
        signed = True
    number = word - 0x10000 if signed and word & 0x8000 else word
    number = round(number * factor, 2) if factor != 1 else number
    if entity.kind == "coded":
        return Value(entity, number, entity.coding.get(number, "unknown code"))
    if entity.kind == "boolean" and number not in (0, 1):
        return Value(entity, text=f"invalid ({number})")
    return Value(entity, number)


def fetch(args, entities):
    client = ModbusClient(args.host, args.port, args.unit_id, args.timeout)
    try:
        words = read_words(client, entities)
    finally:
        client.close()
    return {entity.name: decode(entity, words) for entity in entities}


# ---------------------------------------------------------------------------
# entity listing (--list)
# ---------------------------------------------------------------------------

def print_entity_list(values, host, show_all):
    model = values.get("controller_model")
    heading = f"Values of {host}"
    if model is not None and model.available:
        heading += f", controller {model.text}"
    print(heading + ":")
    missing = 0
    for block, (low, high) in (("system values", (501, 608)), ("system parameters", (1501, 1521)),
                               ("system status", (2501, 2547)), ("energy data", (3501, 3655))):
        lines = []
        for entity in ENTITIES:
            if not low <= entity.register <= high:
                continue
            value = values[entity.name]
            if not value.available and not show_all:
                missing += 1
                continue
            lines.append(f"    {entity.name} = {value.display()}  [{entity.register}: {entity.label}]")
        if lines:
            print(f"  {block}:")
            print("\n".join(lines))
    if missing:
        print(f"  ({missing} more values are not available on this controller; --all lists them too)")


# ---------------------------------------------------------------------------
# threshold evaluation (direction-agnostic: -c may be smaller than -w)
# ---------------------------------------------------------------------------

def format_value(value):
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.6g}"


def shown_unit(uom):
    return f" {uom}" if uom and uom not in ("%",) else uom


def perf_unit(uom):
    return uom if uom in PERFDATA_UOMS else ""


def evaluate_numeric(value, warning, critical, label, uom=""):
    if warning is not None and critical is not None and warning > critical:
        # lower values are worse (e.g. a pressure that must not drop)
        if value <= critical:
            state = STATE_CRITICAL
        elif value <= warning:
            state = STATE_WARNING
        else:
            state = STATE_OK
    else:
        # higher values are worse (the common case)
        if critical is not None and value >= critical:
            state = STATE_CRITICAL
        elif warning is not None and value >= warning:
            state = STATE_WARNING
        else:
            state = STATE_OK

    fields = ["" if warning is None else format_value(warning), "" if critical is None else format_value(critical)]
    while fields and fields[-1] == "":
        fields.pop()
    perf = f"'{label}'={format_value(value)}{perf_unit(uom)}" + "".join(";" + field for field in fields)
    return state, f"{label} is {format_value(value)}{shown_unit(uom)} | {perf}"


def evaluate_margin(value, other, must_be, warning, critical, label, other_label, uom=""):
    """--check compare: value has to be above (or below) other by more than
    the margins -w and -c; a margin at or below -c is CRITICAL."""
    margin = round(value - other if must_be == "above" else other - value, 6)
    if margin <= critical:
        state = STATE_CRITICAL
    elif margin <= warning:
        state = STATE_WARNING
    else:
        state = STATE_OK
    # a margin between two temperatures is a temperature difference
    margin_uom = "K" if uom == "°C" else uom
    if margin > 0:
        relation = f"{format_value(abs(margin))}{shown_unit(margin_uom)} {must_be}"
    elif margin == 0:
        relation = f"equal to, not {must_be},"
    else:
        relation = (f"{format_value(abs(margin))}{shown_unit(margin_uom)} "
                    f"{'below' if must_be == 'above' else 'above'}, not {must_be},")
    unit = perf_unit(uom)
    message = (f"{label} is {relation} {other_label} ({format_value(value)}{shown_unit(uom)} vs "
               f"{format_value(other)}{shown_unit(uom)}) | 'margin'={format_value(margin)}{unit};"
               f"{format_value(warning)};{format_value(critical)} '{label}'={format_value(value)}{unit} "
               f"'{other_label}'={format_value(other)}{unit}")
    return state, message


# ---------------------------------------------------------------------------
# on/off logical expression check
# ---------------------------------------------------------------------------

ALLOWED_EXPR_NODES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.Name, ast.Attribute, ast.Load,
    ast.Constant,
)


def parse_bool_expr(expr):
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        die(STATE_UNKNOWN, f"Invalid --binary-expr: {exc}")

    def check(node):
        if not isinstance(node, ALLOWED_EXPR_NODES):
            die(STATE_UNKNOWN, f"Unsupported element in --binary-expr: {type(node).__name__}")
        if isinstance(node, ast.Attribute) and not isinstance(node.value, ast.Name):
            die(STATE_UNKNOWN, "Unsupported element in --binary-expr: write status bits as register.bit")
        for child in ast.iter_child_nodes(node):
            check(child)

    check(tree)
    return tree


def name_of(node):
    if isinstance(node, ast.Attribute):
        return f"{node.value.id}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return node.value


def extract_names(tree):
    names = set()

    def walk(node):
        if isinstance(node, (ast.Attribute, ast.Name)) or (
                isinstance(node, ast.Constant) and isinstance(node.value, str)):
            names.add(name_of(node))
            return
        for child in ast.iter_child_nodes(node):
            walk(child)

    walk(tree)
    return sorted(names)


def eval_bool_expr(tree, values):
    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.BoolOp):
            results = [ev(v) for v in node.values]
            return all(results) if isinstance(node.op, ast.And) else any(results)
        if isinstance(node, ast.UnaryOp):
            return not ev(node.operand)
        if isinstance(node, ast.Constant) and not isinstance(node.value, str):
            return bool(node.value)
        return values[name_of(node)]

    return ev(tree)


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

class ArgumentParser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error, which Nagios reads as CRITICAL; exit UNKNOWN instead."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(STATE_UNKNOWN, f"{self.prog}: error: {message}\n")


def parse_args(argv=None):
    parser = ArgumentParser(
        prog="check_isg_wpm_sensor",
        description="Check a Stiebel Eltron heat pump with WPM via the Modbus TCP interface of its ISG.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s -H servicewelt.local --check sensor --entity outside_temperature -w 35 -c 40\n"
            "  %(prog)s -H servicewelt.local --check sensor --entity heating_pressure -w 1.2 -c 1.0\n"
            "  %(prog)s -H servicewelt.local --check compare --entity actual_temperature_dhw "
            "--compare-entity set_temperature_dhw --must-be above -w -5 -c -10\n"
            "  %(prog)s -H servicewelt.local --check binary --binary-expr 'fault_output'\n"
            "  %(prog)s -H servicewelt.local --check text --entity operating_mode --regex '^(program|comfort|eco)$'\n"
            "  %(prog)s -H servicewelt.local --check fault\n"
            "  %(prog)s -H servicewelt.local --list\n"
        ),
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-H", "--host", required=True, help="ISG hostname or IP address")
    parser.add_argument("-p", "--port", type=int, default=502, help="Modbus TCP port (default: 502)")
    parser.add_argument("--unit-id", type=int, default=1, help="Modbus unit id (default: 1, fixed on the ISG)")
    parser.add_argument("-t", "--timeout", type=float, default=10, help="timeout in seconds (default: 10)")

    parser.add_argument("--list", action="store_true",
                        help="list the values the controller provides and exit, instead of running a check")
    parser.add_argument("--all", action="store_true", help="with --list, also list values that are not available")
    parser.add_argument("--check", choices=("sensor", "compare", "binary", "text", "fault"),
                        help="type of check to perform")
    parser.add_argument("--entity", help="name of the value (see --list), or its register number")
    parser.add_argument("--unavailable-state", choices=("ok", "warning", "critical", "unknown"), default="unknown",
                        help="state if the controller does not provide a value (default: unknown)")
    parser.add_argument("-w", "--warning", type=float, help="warning threshold (may be > or < -c)")
    parser.add_argument("-c", "--critical", type=float, help="critical threshold (may be > or < -w)")

    compare_group = parser.add_argument_group("--check compare (also uses --entity, -w, -c)")
    compare_group.add_argument("--compare-entity", help="value --entity is compared with")
    compare_group.add_argument(
        "--must-be", choices=("above", "below"),
        help="whether --entity has to stay above or below --compare-entity; -w and -c are the smallest "
             "acceptable margins, so -w must not be smaller than -c",
    )

    binary_group = parser.add_argument_group("--check binary")
    binary_group.add_argument(
        "--binary-expr",
        help="boolean expression over on/off values, e.g. 'operating_status.compressor and not evu_release'",
    )
    binary_group.add_argument("--true-state", choices=("ok", "warning", "critical"), default="critical")
    binary_group.add_argument("--false-state", choices=("ok", "warning", "critical"), default="ok")

    text_group = parser.add_argument_group("--check text")
    text_group.add_argument("--regex", help="Python regular expression to search for in the value")
    text_group.add_argument("-i", "--ignore-case", action="store_true", help="match --regex case-insensitively")
    text_group.add_argument("--match-state", choices=("ok", "warning", "critical"), default="ok")
    text_group.add_argument("--no-match-state", choices=("ok", "warning", "critical"), default="critical")

    args = parser.parse_args(argv)

    if args.list:
        return args
    if not args.check:
        parser.error("--check is required (or use --list)")
    if args.check == "sensor":
        if not args.entity or args.warning is None or args.critical is None:
            parser.error("--check sensor requires --entity, -w and -c")
    elif args.check == "compare":
        if not args.entity or not args.compare_entity or not args.must_be or args.warning is None \
                or args.critical is None:
            parser.error("--check compare requires --entity, --compare-entity, --must-be, -w and -c")
        if args.warning < args.critical:
            parser.error("for --check compare, -w and -c are the smallest acceptable margins, "
                         "so -w must not be smaller than -c")
    elif args.check == "binary":
        if not args.binary_expr:
            parser.error("--check binary requires --binary-expr")
    elif args.check == "text":
        if not args.entity or not args.regex:
            parser.error("--check text requires --entity and --regex")
        try:
            re.compile(args.regex, re.IGNORECASE if args.ignore_case else 0)
        except re.error as exc:
            parser.error(f"Invalid --regex: {exc}")
    return args


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def require(args, value):
    if not value.available:
        die(STATE_BY_NAME[args.unavailable_state],
            f"{value.entity.name} (register {value.entity.register}) is {value.text or 'not available'}")
    return value


def main(argv=None):
    args = parse_args(argv)

    if args.list:
        print_entity_list(fetch(args, ENTITIES), args.host, args.all)
        sys.exit(STATE_OK)

    if args.check == "sensor":
        entity = find_entity(args.entity)
        value = require(args, fetch(args, [entity])[entity.name])
        state, message = evaluate_numeric(value.number, args.warning, args.critical, entity.name, entity.unit)
        if value.text:
            message = message.replace(f" is {format_value(value.number)}", f" is {value.text} ({format_value(value.number)})", 1)
        die(state, message)

    elif args.check == "compare":
        entity, other = find_entity(args.entity), find_entity(args.compare_entity)
        values = fetch(args, [entity, other])
        value, other_value = require(args, values[entity.name]), require(args, values[other.name])
        if entity.unit != other.unit:
            die(STATE_UNKNOWN, f"{entity.name} is in {entity.unit or 'no unit'}, "
                               f"but {other.name} in {other.unit or 'no unit'}")
        state, message = evaluate_margin(value.number, other_value.number, args.must_be, args.warning,
                                         args.critical, entity.name, other.name, entity.unit)
        die(state, message)

    elif args.check == "binary":
        tree = parse_bool_expr(args.binary_expr)
        names = extract_names(tree)
        if not names:
            die(STATE_UNKNOWN, "No value names found in --binary-expr")
        entities = [find_entity(name) for name in names]
        for entity in entities:
            if entity.kind not in ("boolean", "bit"):
                die(STATE_UNKNOWN, f"{entity.name} is not an on/off value")
        values = fetch(args, entities)
        flags = {entity.name: bool(require(args, values[entity.name]).number) for entity in entities}
        result = eval_bool_expr(tree, {name: flags[find_entity(name).name] for name in names})
        state = STATE_BY_NAME[args.true_state if result else args.false_state]
        details = ", ".join(f"{name}={'ON' if flags[find_entity(name).name] else 'OFF'}" for name in names)
        die(state, f"'{args.binary_expr}' is {result} ({details})")

    elif args.check == "text":
        entity = find_entity(args.entity)
        value = require(args, fetch(args, [entity])[entity.name])
        text = value.text if value.text else format_value(value.number)
        flags = re.IGNORECASE if args.ignore_case else 0
        matched = re.search(args.regex, text, flags) is not None
        state = STATE_BY_NAME[args.match_state if matched else args.no_match_state]
        die(state, f"{entity.name} = {text!r} {'matches' if matched else 'does not match'} /{args.regex}/")

    elif args.check == "fault":
        names = ("fault_status", "bus_status", "active_error", "message_number")
        values = fetch(args, [ENTITY_BY_NAME[name] for name in names])
        fault, bus = values["fault_status"], values["bus_status"]
        if not fault.available and not bus.available:
            die(STATE_UNKNOWN, "The controller provides neither the fault status nor the bus status")
        problems = []
        if fault.available and fault.number:
            detail = "the heat pump reports a fault"
            for name, what in (("active_error", "active error"), ("message_number", "message number")):
                if values[name].available and values[name].number:
                    detail += f", {what} {format_value(values[name].number)}"
            problems.append(detail)
        if bus.available and bus.number != 0:
            problems.append(f"bus status {bus.text} ({format_value(bus.number)})")
        perf = []
        if fault.available:
            perf.append(f"'fault'={format_value(fault.number)};;1")
        if bus.available:
            perf.append(f"'bus_status'={format_value(bus.number)}")
        summary = "; ".join(problems) if problems else "no fault" + (", bus ok" if bus.available else "")
        die(STATE_CRITICAL if problems else STATE_OK, f"{summary} | {' '.join(perf)}")


def run():
    try:
        main()
    except KeyboardInterrupt:
        die(STATE_UNKNOWN, "Interrupted")


if __name__ == "__main__":
    run()
