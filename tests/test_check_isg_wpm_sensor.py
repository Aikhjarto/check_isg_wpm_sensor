"""Tests for check_isg_wpm_sensor against a fake ISG Modbus TCP server."""
import os
import socketserver
import struct
import subprocess
import sys
import threading
import unittest

import check_isg_wpm_sensor as plugin

SRC = os.path.dirname(os.path.dirname(os.path.abspath(plugin.__file__)))

# registers as documented (1 based) -> raw word, taken from a WPMsystem
REGISTERS = {
    ("input", 501): 0x8000,
    ("input", 507): 175,       # outside temperature 17.5 °C
    ("input", 508): 327,
    ("input", 510): 230,
    ("input", 516): 236,       # return 23.6 °C
    ("input", 515): 0x8000,
    ("input", 513): 332,       # flow of the heat pump 33.2 °C
    ("input", 517): 0x9000,    # fixed value operation off
    ("input", 520): 150,       # heating pressure 1.50 bar (data type 7)
    ("input", 522): 630,       # hot water 63.0 °C
    ("input", 523): 550,
    ("input", 524): 0x8004,    # a code, not -3276.4 K
    ("input", 536): 65436,     # source temperature -10.0 °C
    ("input", 545): 771,
    ("input", 588): 194,
    ("input", 590): 449,
    ("input", 591): 71,
    ("holding", 1501): 2,      # operating mode: program
    ("holding", 1504): 20,     # heating curve rise 0.20
    ("holding", 1508): 0x9000,
    ("holding", 1509): 65436,
    ("input", 2501): (1 << 6) | (1 << 4),  # compressor and heating
    ("input", 2502): 1,
    ("input", 2504): 0,
    ("input", 2505): 0,
    ("input", 2507): 0,
    ("input", 2508): 0,
    ("input", 2509): 1,
    ("input", 2516): 0,
    ("input", 3502): 491,      # 491 kWh + 2 MWh
    ("input", 3503): 2,
    ("input", 3505): 200,
    ("input", 3506): 0x8000,
    ("input", 5002): 449,
}


class FakeISG(socketserver.BaseRequestHandler):
    """Answers function codes 3 and 4 like the ISG; registers not in the map are illegal addresses."""

    def handle(self):
        while True:
            request = self.request.recv(12)
            if len(request) < 12:
                return
            transaction, _, _, unit, function, address, count = struct.unpack(">HHHBBHH", request)
            space = {4: "input", 3: "holding"}.get(function)
            self.server.log.append((space, address + 1, count))
            words = [REGISTERS.get((space, address + 1 + offset)) for offset in range(count)]
            if space is None or unit != 1 or any(word is None for word in words):
                pdu = struct.pack(">BB", function | 0x80, 2 if space else 1)
            else:
                pdu = struct.pack(f">BB{count}H", function, 2 * count, *words)
            self.request.sendall(struct.pack(">HHHB", transaction, 0, len(pdu) + 1, unit) + pdu)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class TestPlugin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server(("127.0.0.1", 0), FakeISG)
        cls.server.log = []
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]
        # fill the documented registers the fake does not know with "not available",
        # so that the pooled reads of --list succeed like on a real ISG
        for entity in plugin.ENTITIES:
            for register in entity.registers:
                REGISTERS.setdefault((entity.space, register), 0x8000)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.server.log.clear()

    def run_plugin(self, *argv, port=None):
        return subprocess.run(
            [sys.executable, "-m", "check_isg_wpm_sensor", "-H", "127.0.0.1", "-p", str(port or self.port)]
            + list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
            env=dict(os.environ, PYTHONPATH=SRC), timeout=60)

    def expect(self, state, pattern, *argv, **kwargs):
        result = self.run_plugin(*argv, **kwargs)
        self.assertEqual(state, result.returncode, result)
        self.assertRegex(result.stdout, pattern)
        self.assertEqual(1, len(result.stdout.splitlines()), result)
        return result

    # --check sensor

    def test_sensor(self):
        self.expect(plugin.STATE_OK, r"^OK: outside_temperature is 17.5 °C \| 'outside_temperature'=17.5;35;40$",
                    "--check", "sensor", "--entity", "outside_temperature", "-w", "35", "-c", "40")
        # the documented register 507 is read at protocol address 506 with function code 4
        self.assertEqual([("input", 507, 1)], self.server.log)

    def test_sensor_by_register_number(self):
        self.expect(plugin.STATE_OK, r"^OK: actual_temperature_dhw is 63 °C", "--check", "sensor", "--entity", "522",
                    "-w", "70", "-c", "80")

    def test_sensor_lower_is_worse(self):
        self.expect(plugin.STATE_WARNING, r"^WARNING: heating_pressure is 1.5 bar \| 'heating_pressure'=1.5;1.6;1$",
                    "--check", "sensor", "--entity", "heating_pressure", "-w", "1.6", "-c", "1.0")
        self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: ", "--check", "sensor", "--entity", "heating_pressure",
                    "-w", "2", "-c", "1.5")

    def test_negative_value(self):
        self.expect(plugin.STATE_OK, r"^OK: source_temperature is -10 °C", "--check", "sensor", "--entity",
                    "source_temperature", "-w", "-15", "-c", "-20")

    def test_holding_register_and_coded_value(self):
        self.expect(plugin.STATE_OK, r"^OK: operating_mode is program \(2\)", "--check", "sensor", "--entity",
                    "operating_mode", "-w", "4", "-c", "5")
        self.assertEqual([("holding", 1501, 1)], self.server.log)

    def test_percent_in_perfdata(self):
        self.expect(plugin.STATE_OK, r"'room_humidity_hk_2'=44.9%;60;70$", "--check", "sensor", "--entity",
                    "room_humidity_hk_2", "-w", "60", "-c", "70")

    def test_energy_total(self):
        self.expect(plugin.STATE_OK, r"^OK: vd_heating_total is 2491 kWh", "--check", "sensor", "--entity",
                    "vd_heating_total", "-w", "10000", "-c", "20000")
        self.assertEqual([("input", 3502, 2)], self.server.log)

    def test_not_available(self):
        self.expect(plugin.STATE_UNKNOWN, r"^UNKNOWN: actual_temperature_fe7 \(register 501\) is not available$",
                    "--check", "sensor", "--entity", "actual_temperature_fe7", "-w", "1", "-c", "2")
        self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: vd_dhw_total \(register 3505\) is not available$",
                    "--check", "sensor", "--entity", "vd_dhw_total", "-w", "1", "-c", "2",
                    "--unavailable-state", "critical")

    def test_codes_are_not_values(self):
        self.expect(plugin.STATE_UNKNOWN, r"is not available \(0x8004\)$", "--check", "sensor", "--entity",
                    "actual_temperature_fan", "-w", "1", "-c", "2")
        self.expect(plugin.STATE_UNKNOWN, r"^UNKNOWN: fixed_value_operation \(register 1508\) is off$",
                    "--check", "sensor", "--entity", "fixed_value_operation", "-w", "1", "-c", "2")

    def test_illegal_address(self):
        del REGISTERS[("input", 508)]
        try:
            self.expect(plugin.STATE_UNKNOWN, r"is not served \(illegal data address\)$", "--check", "sensor",
                        "--entity", "actual_temperature_hk_1", "-w", "1", "-c", "2")
        finally:
            REGISTERS[("input", 508)] = 327

    # --check compare

    def test_compare(self):
        self.expect(plugin.STATE_OK, r"^OK: actual_flow_temperature_wp is 9.6 K above actual_return_temperature "
                                     r"\(33.2 °C vs 23.6 °C\) \| 'margin'=9.6;5;2 ",
                    "--check", "compare", "--entity", "actual_flow_temperature_wp", "--compare-entity",
                    "actual_return_temperature", "--must-be", "above", "-w", "5", "-c", "2")
        # one read covers both registers, 513 to 516
        self.assertEqual([("input", 513, 4)], self.server.log)

    def test_compare_dew_point(self):
        self.expect(plugin.STATE_WARNING, r"^WARNING: actual_room_temperature_hk_2 is 12.3 K above", "--check",
                    "compare", "--entity", "actual_room_temperature_hk_2", "--compare-entity",
                    "room_dew_point_temperature_hk_2", "--must-be", "above", "-w", "13", "-c", "5")

    def test_compare_wrong_side(self):
        self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: actual_temperature_dhw is 8 K above, not below, "
                                           r"set_temperature_dhw", "--check", "compare", "--entity",
                    "actual_temperature_dhw", "--compare-entity", "set_temperature_dhw", "--must-be", "below",
                    "-w", "2", "-c", "0")

    def test_compare_different_units(self):
        self.expect(plugin.STATE_UNKNOWN, r"^UNKNOWN: heating_pressure is in bar, but outside_temperature in °C$",
                    "--check", "compare", "--entity", "heating_pressure", "--compare-entity", "outside_temperature",
                    "--must-be", "above", "-w", "1", "-c", "0")

    # --check binary

    def test_binary(self):
        self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: 'operating_status.compressor and evu_release' is True "
                                           r"\(evu_release=ON, operating_status.compressor=ON\)$",
                    "--check", "binary", "--binary-expr", "operating_status.compressor and evu_release")
        self.expect(plugin.STATE_OK, r"^OK: 'fault_output or fault_status' is False",
                    "--check", "binary", "--binary-expr", "fault_output or fault_status")

    def test_binary_bits(self):
        self.expect(plugin.STATE_OK, r"is False \(operating_status.dhw=OFF\)$", "--check", "binary", "--binary-expr",
                    "operating_status.dhw")

    def test_binary_needs_on_off_values(self):
        self.expect(plugin.STATE_UNKNOWN, r"^UNKNOWN: outside_temperature is not an on/off value$",
                    "--check", "binary", "--binary-expr", "outside_temperature")
        self.expect(plugin.STATE_UNKNOWN, r"Unsupported element in --binary-expr: Call",
                    "--check", "binary", "--binary-expr", "__import__('os')")

    # --check text

    def test_text(self):
        self.expect(plugin.STATE_OK, r"^OK: operating_mode = 'program' matches", "--check", "text", "--entity",
                    "operating_mode", "--regex", "^(program|comfort|eco)$")
        self.expect(plugin.STATE_CRITICAL, r"does not match", "--check", "text", "--entity", "bus_status",
                    "--regex", "error")

    # --check fault

    def test_fault_ok(self):
        self.expect(plugin.STATE_OK, r"^OK: no fault, bus ok \| 'fault'=0;;1 'bus_status'=0$", "--check", "fault")

    def test_fault(self):
        REGISTERS[("input", 2504)], REGISTERS[("input", 2507)], REGISTERS[("input", 2505)] = 1, 12, 0xFFFD
        try:
            self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: the heat pump reports a fault, active error 12; "
                                               r"bus status bus off \(-3\) \|", "--check", "fault")
        finally:
            REGISTERS[("input", 2504)], REGISTERS[("input", 2507)], REGISTERS[("input", 2505)] = 0, 0, 0

    # --list

    def test_list(self):
        result = self.run_plugin("--list")
        self.assertEqual(plugin.STATE_OK, result.returncode, result)
        lines = result.stdout.splitlines()
        self.assertEqual("Values of 127.0.0.1, controller WPMsystem:", lines[0])
        self.assertIn("    outside_temperature = 17.5 °C  [507: AUSSENTEMPERATUR]", lines)
        self.assertIn("    operating_mode = program (2)  [1501: BETRIEBSART]", lines)
        self.assertIn("    operating_status.compressor = ON  [2501: BETRIEBSSTATUS B6 VERDICHTER IN BETRIEB]", lines)
        self.assertNotIn("actual_temperature_fe7", result.stdout)
        result = self.run_plugin("--list", "--all")
        self.assertIn("    actual_temperature_fe7 = not available  [501: ISTTEMPERATUR FE7]", result.stdout.splitlines())

    # connection and command line

    def test_unreachable_is_critical(self):
        self.expect(plugin.STATE_CRITICAL, r"^CRITICAL: Cannot connect to the ISG at 127.0.0.1:1: ", "--check",
                    "fault", port=1)

    def test_unknown_entity(self):
        self.expect(plugin.STATE_UNKNOWN, r"^UNKNOWN: Unknown entity: nope \(see --list\)$", "--check", "sensor",
                    "--entity", "nope", "-w", "1", "-c", "2")

    def test_usage_errors_are_unknown(self):
        for argv in (["-Z"], [], ["--check", "sensor"], ["--check", "nope"], ["-p", "abc", "--list"],
                     ["--check", "compare", "--entity", "a", "--compare-entity", "b", "--must-be", "above",
                      "-w", "0", "-c", "2"]):
            result = self.run_plugin(*argv)
            self.assertEqual(plugin.STATE_UNKNOWN, result.returncode, (argv, result))
            self.assertIn("check_isg_wpm_sensor: error: ", result.stderr)


class TestRegisterMap(unittest.TestCase):
    def test_names_are_unique(self):
        names = [entity.name for entity in plugin.ENTITIES]
        self.assertEqual(len(names), len(set(names)))

    def test_documented_blocks_are_covered(self):
        # every register of the documented blocks of section 6 belongs to a value
        used = {(entity.space, register) for entity in plugin.ENTITIES for register in entity.registers}
        for space, first, last in (("input", 501, 608), ("holding", 1501, 1521), ("input", 2501, 2547),
                                   ("input", 3501, 3655)):
            for register in range(first, last + 1):
                self.assertIn((space, register), used, register)

    def test_heat_pump_and_energy_offsets(self):
        byname = plugin.ENTITY_BY_NAME
        self.assertEqual(577, byname["return_temperature_hp_6"].register)
        self.assertEqual(583, byname["water_flow_rate_hp_6"].register)
        self.assertEqual(600, byname["actual_room_temperature_hk_5"].register)
        self.assertEqual(3522, byname["runtime_nhz_1_2"].register)
        self.assertEqual(3546, byname["runtime_reheating_1"].register)
        self.assertEqual(3549, byname["vd_heating_day_hp_2"].register)
        self.assertEqual(3625, byname["vd_heating_day_hp_6"].register)
        self.assertEqual(3643, byname["runtime_vd_cooling_hp_6"].register)
        self.assertEqual(3655, byname["runtime_dhw_hp_6"].register)

    def test_spans(self):
        self.assertEqual([[1, 3], [20, 20]], plugin.spans([3, 1, 2, 20]))
        self.assertEqual(2, len(plugin.spans(range(3501, 3656))))


if __name__ == "__main__":
    unittest.main()
