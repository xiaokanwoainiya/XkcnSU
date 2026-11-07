"""Host-only tests for root-policy probe dispatch and shared result validation."""

from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "root_policy_acceptance", Path(__file__).with_name("root-policy-acceptance.py"))
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class RootPolicyAcceptanceTests(unittest.TestCase):
    def run_probe(self, foreground=False, expected="granted", event_uid=10234):
        nonce = "0123456789abcdef0123456789abcdef"
        event = {
            "nonce": nonce, "uid": event_uid, "timed_out": False, "error": None,
            "stdout_complete": True, "stderr_complete": True,
            "su_exit": 0 if expected == "granted" else 1,
            "stdout": "uid=0(root) context=u:r:ksu:s0" if expected == "granted" else "",
        }
        stale = dict(event, nonce="previous-probe", uid=99999)
        calls = []

        def adb(command, **options):
            self.assertEqual(command[:3], ["adb", "-s", "TEST-SERIAL"])
            self.assertEqual(options["timeout"], 20)
            calls.append(command[3:])
            self.assertEqual(command[3], "shell")
            self.assertEqual(len(command), 5)
            arguments = shlex.split(command[4])
            if arguments[:3] == ["pm", "list", "packages"]:
                text = f"package:{RUNNER.PACKAGE} uid:10234\n"
            elif arguments[:1] == ["am"]:
                text = "Starting: Intent\n" if foreground else "Broadcast completed: result=0\n"
            else:
                self.assertEqual(arguments[:2], ["su", "-c"])
                self.assertEqual(len(arguments), 3)
                self.assertEqual(shlex.split(arguments[2]), [
                    "logcat", "-d", "-b", "all", "-v", "raw",
                    "-s", "XKCNSUAcceptance:I", "*:S",
                ])
                text = "\n".join([json.dumps(event), "unrelated log", json.dumps(stale)])
            return subprocess.CompletedProcess(command, 0, text, "")

        with tempfile.TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "report.json"
            arguments = ["root-policy-acceptance.py", "--serial", "TEST-SERIAL",
                         "--expect", expected, "--report", str(report_path)]
            if foreground:
                arguments.append("--foreground")
            with patch.object(sys, "argv", arguments), \
                    patch.object(RUNNER.uuid, "uuid4") as generated, \
                    patch.object(RUNNER.subprocess, "run", side_effect=adb), \
                    redirect_stdout(io.StringIO()):
                generated.return_value.hex = nonce
                result = RUNNER.main()
            report = json.loads(report_path.read_text(encoding="utf-8"))
        return result, report, calls

    def test_default_broadcast_mode_is_preserved(self):
        result, report, calls = self.run_probe()
        self.assertEqual(result, 0)
        self.assertTrue(report["completed"])
        self.assertEqual(report["mode"], "broadcast")
        self.assertEqual(shlex.split(calls[1][1]), [
            "am", "broadcast", "--include-stopped-packages", "--receiver-foreground",
            "-a", RUNNER.PACKAGE + ".PROBE", "-n", RUNNER.PACKAGE + "/.ProbeReceiver",
            "--es", "nonce", report["nonce"],
        ])

    def test_foreground_launch_uses_force_stop_and_exact_nonce_validation(self):
        result, report, calls = self.run_probe(foreground=True)
        self.assertEqual(result, 0)
        self.assertTrue(report["completed"])
        self.assertEqual(report["mode"], "foreground")
        self.assertEqual(shlex.split(calls[1][1]), [
            "am", "start", "-S", "-n", RUNNER.PACKAGE + "/.ProbeActivity",
            "--es", "nonce", report["nonce"],
        ])
        self.assertEqual(report["probe"]["nonce"], report["nonce"])
        self.assertEqual(report["probe"]["uid"], 10234)
        self.assertTrue(all(check["passed"] for check in report["checks"]))
        self.assertEqual(len(calls), 3)

    def test_foreground_denied_probe_reuses_denial_validation(self):
        result, report, _ = self.run_probe(foreground=True, expected="denied")
        self.assertEqual(result, 0)
        self.assertTrue(report["completed"])
        self.assertEqual(report["mode"], "foreground")
        self.assertEqual(report["probe"]["su_exit"], 1)
        self.assertEqual(report["checks"][-1]["description"],
                         "The unauthorized app was denied root")

    def test_foreground_launch_acknowledgement_does_not_override_wrong_uid(self):
        result, report, _ = self.run_probe(foreground=True, event_uid=2000)
        self.assertEqual(result, 1)
        self.assertFalse(report["completed"])
        self.assertEqual(report["mode"], "foreground")
        self.assertEqual(report["error"], "The probe belongs to the selected installed app")

    def test_both_modes_read_all_logs_as_root_with_literal_filters(self):
        for foreground in (False, True):
            with self.subTest(foreground=foreground):
                result, report, calls = self.run_probe(foreground=foreground)
                self.assertEqual(result, 0)
                self.assertIs(report["read_log_as_root"], True)
                log_command = calls[-1]
                self.assertEqual(log_command[0], "shell")
                outer = shlex.split(log_command[1])
                self.assertEqual(outer[:2], ["su", "-c"])
                self.assertEqual(len(outer), 3)
                self.assertEqual(outer[2],
                                 "logcat -d -b all -v raw -s XKCNSUAcceptance:I '*:S'")
                self.assertEqual(shlex.split(outer[2])[-2:],
                                 ["XKCNSUAcceptance:I", "*:S"])


if __name__ == "__main__":
    unittest.main()
