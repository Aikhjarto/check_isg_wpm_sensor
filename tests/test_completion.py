"""Tests for the bash completion in completions/check_isg_wpm_sensor."""
import os
import re
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLUGIN = os.path.join(ROOT, "src", "check_isg_wpm_sensor", "__init__.py")
COMPLETION = os.path.join(ROOT, "completions", "check_isg_wpm_sensor")


def complete(*words, command="check_isg_wpm_sensor"):
    """The candidates bash offers for the last of words, which may be empty."""
    words = (command,) + words
    script = 'source "$1"; shift; COMP_WORDS=("$@"); COMP_CWORD=$(($# - 1)); ' \
             '$(complete -p "$1" | sed -E "s/.*-F ([^ ]+) .*/\\1/"); printf "%s\\n" "${COMPREPLY[@]}"'
    result = subprocess.run(["bash", "-c", script, "bash", COMPLETION] + list(words),
                            capture_output=True, text=True, check=True)
    return [line for line in result.stdout.split("\n") if line]


def help_options():
    """The option strings --help lists.

    Before Python 3.13 argparse repeats the metavar after every option string
    ("-H HOST, --host HOST"), since then only after the last ("-H, --host HOST").
    An option line starts with the invocation, followed by the description after
    two or more spaces or, if the invocation is long, on the next line.
    """
    text = subprocess.run([sys.executable, PLUGIN, "--help"], capture_output=True, text=True,
                          check=True).stdout
    options = set()
    for line in text.split("\n"):
        match = re.match(r"  (-\S.*?)(?:\s{2,}|$)", line)
        if match:
            options.update(re.findall(r"(?:^|, )(-{1,2}[^\s,=\[]+)", match.group(1)))
    return options


@unittest.skipUnless(shutil.which("bash"), "bash is not installed")
class CompletionTests(unittest.TestCase):
    def test_syntax(self):
        subprocess.run(["bash", "-n", COMPLETION], check=True)

    def test_offers_exactly_the_options_of_help(self):
        self.assertEqual(set(complete("")), help_options())

    def test_option_prefix(self):
        self.assertEqual(complete("--u"), ["--unit-id", "--unavailable-state"])

    def test_choices(self):
        self.assertEqual(complete("--check", "f"), ["fault"])
        self.assertEqual(complete("--unavailable-state", "u"), ["unknown"])
        self.assertEqual(complete("--must-be", "b"), ["below"])
        for option in ("--true-state", "--false-state", "--match-state", "--no-match-state"):
            self.assertEqual(complete(option, ""), ["ok", "warning", "critical"], option)

    def test_equals_form(self):
        self.assertEqual(complete("--check", "=", "b"), ["binary"])
        self.assertEqual(complete("--must-be", "="), ["above", "below"])

    def test_no_candidates_for_values(self):
        for option in ("-w", "--critical", "--entity", "--regex", "--unit-id", "-p"):
            self.assertEqual(complete(option, ""), [], option)


if __name__ == "__main__":
    unittest.main()
