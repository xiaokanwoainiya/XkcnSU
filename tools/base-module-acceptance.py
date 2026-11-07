#!/usr/bin/env python3
"""Explicit, reboot-separated ordinary-module acceptance; Python stdlib only.

Read cli.rs (Module), module.rs (install/list/enable/disable/uninstall), and
init_event.rs: install stages an update, scripts run at boot, and uninstall
marks removal for the next boot. Never invoke init events or reboot here.

Each device invocation requires --serial, --ksud, --execute, --state, --report,
and --phase. Use a fresh state path for prepare, reboot externally, run
after-boot, run cleanup, reboot externally, then run cleanup again.
--with-webui adds a static resource to the dedicated test module.

Only the official module CLI mutates the module store. Direct device writes
are confined to /data/adb/ksu/XKCNSU/acceptance/<uuid4 hex>/ (including upload).
The shared acceptance parent is created if missing. CLI-managed installer,
logging and preinit side effects follow ksud's normal implementation.
Inventories are recorded as fingerprints, never as user module contents.
Failure preserves state, command outcomes and reports; no automatic rollback.
Run --self-test for entirely local checks with a fake device transport.
"""

import argparse
import base64
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile


PREFIX = "xkcnsu_acceptance_"
BASE = "/data/adb/ksu/XKCNSU/acceptance"
FLAGS = ("enabled", "update", "remove", "web", "action", "mount")
SCHEMA = 1


def digest(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def save_json(path, value):
    """Replace only this explicitly selected local state/report file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=True, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def identity(test_id):
    if not re.fullmatch(r"[0-9a-f]{32}", test_id):
        raise ValueError("State test_id must be lowercase UUID4 hex")
    parsed = uuid.UUID(hex=test_id)
    if parsed.version != 4 or parsed.variant != uuid.RFC_4122:
        raise ValueError("State test_id must be UUID4")
    return PREFIX + test_id, BASE + "/" + test_id


def fixture(test_id, with_webui=False):
    module_id, directory = identity(test_id)
    props = {
        "id": module_id,
        "name": "XKCNSU ordinary module acceptance",
        "version": "1.0",
        "versionCode": "1",
        "author": "XKCNSU acceptance",
        "description": "Dedicated harmless boot marker fixture " + test_id,
    }
    files = {
        "module.prop": "".join(f"{key}={value}\n" for key, value in props.items()).encode(),
        "skip_mount": b"",
    }
    for stage in ("post-fs-data", "service"):
        # No installer, uninstall hook, system tree, properties or policy changes.
        script = (
            "#!/system/bin/sh\nset -eu\numask 077\n"
            f"directory={shlex.quote(directory)}\n"
            '[ -d "$directory" ] && [ ! -L "$directory" ] || exit 1\n'
            f'[ ! -L "$directory/{stage}" ] || exit 1\n'
            "boot_id=$(cat /proc/sys/kernel/random/boot_id)\n"
            '[ -n "$boot_id" ] || exit 1\n'
            f"printf '%s\\n' {shlex.quote(test_id + ':' + stage + ':')}"
            f'"$boot_id" > "$directory/{stage}"\n'
        )
        files[stage + ".sh"] = script.encode("ascii")
    if with_webui:
        files["webroot/index.html"] = (
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            "<title>XKCNSU acceptance</title></head><body>"
            + test_id + "</body></html>\n"
        ).encode("ascii")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for name, data in files.items():
            entry = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | (0o755 if name.endswith(".sh") else 0o644)) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            output.writestr(entry, data)
    return archive.getvalue(), files, props


class Acceptance:
    def __init__(self, args, runner=None):
        self.args = args
        self.runner = runner or subprocess.run
        self.state = None
        self.report = {
            "serial": args.serial, "ksud": args.ksud, "phase": args.phase,
            "state": str(args.state.resolve()), "completed": False,
            "commands": [], "checks": [], "started_unix": time.time(),
            "reboot_required": False, "cleanup_verified": False,
        }

    def check(self, condition, description):
        self.report["checks"].append({"description": description, "passed": bool(condition)})
        if not condition:
            raise RuntimeError(description)

    def command(self, arguments, *, private=False, payload=None, timeout=None):
        text = "su -c " + shlex.quote(shlex.join(arguments))
        entry = {"arguments": arguments, "root": True, "exit_code": None}
        self.report["commands"].append(entry)

        def record(stdout, stderr):
            stdout = stdout or b""
            stderr = stderr or b""
            if isinstance(stdout, str):
                stdout = stdout.encode()
            if isinstance(stderr, str):
                stderr = stderr.encode()
            if private:
                entry.update({
                    "output_redacted": "user module inventory",
                    "stdout_sha256": digest(stdout), "stdout_bytes": len(stdout),
                    "stderr_sha256": digest(stderr), "stderr_bytes": len(stderr),
                })
            else:
                entry.update(stdout=stdout.decode("utf-8", "replace"),
                             stderr=stderr.decode("utf-8", "replace"))
            return stdout.decode("utf-8", "replace")

        try:
            result = self.runner(
                ["adb", "-s", self.args.serial, "shell", "-T", text],
                input=payload, capture_output=True, timeout=timeout or self.args.timeout,
            )
        except subprocess.TimeoutExpired as error:
            entry["timed_out"] = True
            record(error.stdout, error.stderr)
            raise RuntimeError("Device command timed out; outcome is unknown, inspect state") from error
        except OSError as error:
            entry["transport_error"] = str(error)
            raise RuntimeError("Device transport could not start; see report") from error
        entry["exit_code"] = result.returncode
        output = record(result.stdout, result.stderr)
        if result.returncode:
            raise RuntimeError(f"Device command failed with exit code {result.returncode}; see report")
        return output.strip()

    def shell(self, script, **kwargs):
        return self.command(["sh", "-c", script], **kwargs)

    def daemon(self, *arguments, **kwargs):
        return self.command([self.args.ksud, "module", *arguments], **kwargs)

    def boot_id(self):
        value = self.command(["cat", "/proc/sys/kernel/random/boot_id"])
        self.check(bool(re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value)),
                   "Kernel boot ID is valid")
        return value

    def modules(self):
        text = self.daemon("list", private=True)
        try:
            rows = json.loads(text)
        except (ValueError, TypeError):
            raise RuntimeError("Module list is not valid JSON; output fingerprint saved") from None
        self.check(isinstance(rows, list), "Module list is a JSON array")
        result = {}
        for row in rows:
            self.check(isinstance(row, dict) and isinstance(row.get("id"), str)
                       and bool(row["id"]) and row["id"] not in result
                       and all(isinstance(v, str) for v in row.values())
                       and all(row.get(flag) in ("true", "false") for flag in FLAGS),
                       "Module entry has a unique ID and documented string flags")
            result[row["id"]] = row
        self.report["module_count"] = len(result)
        return result

    def snapshot(self, rows):
        # All CLI metadata (including flags/preinit/config overrides) participates.
        # Ordering is irrelevant; identifiers and metadata are never printed.
        return {digest(module_id): digest(canonical(row))
                for module_id, row in rows.items() if module_id != self.module_id}

    def disk_snapshot(self):
        # Hash raw module.prop as well: CLI description overrides can hide edits.
        script = (
            "# original-module-fingerprint\nset -eu\n"
            'if [ -e /data/adb/modules ]; then '
            '[ -d /data/adb/modules ] && [ -r /data/adb/modules ] || exit 1; fi\n'
            "for d in /data/adb/modules/* /data/adb/modules/.[!.]* "
            "/data/adb/modules/..?*; do\n"
            '[ -d "$d" ] || continue\n'
            f'[ "$d" != {shlex.quote(self.active)} ] || continue\n'
            'key=$(printf \'%s\' "${d##*/}" | sha256sum) || exit 1\n'
            'key=${key%% *}\nproperty=missing\n'
            'if [ -e "$d/module.prop" ] || [ -L "$d/module.prop" ]; then\n'
            'property=$(sha256sum "$d/module.prop") || exit 1\n'
            'property=${property%% *}\nfi\n'
            'bits=\nfor flag in disable update remove; do\n'
            'if [ -e "$d/$flag" ]; then bits=${bits}1; else bits=${bits}0; fi\n'
            'done\nprintf \'%s %s %s\\n\' "$key" "$property" "$bits"\ndone'
        )
        output = self.shell(script, private=True)
        result = {}
        for line in output.splitlines():
            self.check(bool(re.fullmatch(r"[0-9a-f]{64} (?:[0-9a-f]{64}|missing) [01]{3}", line)),
                       "Original module disk fingerprint is valid")
            key, value = line.split(" ", 1)
            self.check(key not in result, "Original module disk fingerprint is unique")
            result[key] = value
        return result

    def unchanged(self, rows):
        current = self.snapshot(rows)
        baseline = self.state["baseline"]
        self.report["original_modules"] = {
            "expected_count": len(baseline), "observed_count": len(current),
            "expected_sha256": digest(canonical(baseline)),
            "observed_sha256": digest(canonical(current)),
        }
        self.check(current == baseline, "Original module set, metadata and switches are unchanged")
        self.check(self.disk_snapshot() == self.state["baseline_disk"],
                   "Original module.prop bytes and disk switches are unchanged")

    def persist(self):
        self.state["updated_unix"] = time.time()
        save_json(self.args.state, self.state)
        self.report["state_status"] = self.state["status"]

    def use_state(self, state):
        self.state = state
        self.module_id, self.directory = identity(state["test_id"])
        self.active = "/data/adb/modules/" + self.module_id
        self.staged = "/data/adb/modules_update/" + self.module_id
        self.remote_zip = self.directory + "/module.zip"
        self.report.update(test_id=state["test_id"], module_id=self.module_id)

    def load(self):
        state = json.loads(self.args.state.read_text(encoding="utf-8"))
        self.check(state.get("schema") == SCHEMA, "State schema is supported")
        self.check(state.get("serial") == self.args.serial and state.get("ksud") == self.args.ksud,
                   "State is bound to the explicit serial and ksud path")
        self.check(isinstance(state.get("with_webui"), bool)
                   and isinstance(state.get("baseline"), dict)
                   and all(re.fullmatch(r"[0-9a-f]{64}", k)
                           and isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v)
                           for k, v in state["baseline"].items()),
                   "State baseline and fixture options are valid")
        self.check(isinstance(state.get("baseline_disk"), dict)
                   and all(re.fullmatch(r"[0-9a-f]{64}", k) and isinstance(v, str)
                           and re.fullmatch(r"(?:[0-9a-f]{64}|missing) [01]{3}", v)
                           for k, v in state["baseline_disk"].items()),
                   "State raw metadata fingerprints are valid")
        self.use_state(state)

    def own_row(self, rows, *, update=False, enabled=True, remove=False):
        row = rows.get(self.module_id)
        self.check(row is not None, "Dedicated test module is listed")
        _, _, props = fixture(self.state["test_id"], self.state["with_webui"])
        self.check(all(row.get(k) == v for k, v in props.items())
                   and row.get("metamodule") in (None, "0", "false"),
                   "Dedicated module has the exact ordinary fixture metadata")
        for flag, expected in (("enabled", enabled), ("update", update), ("remove", remove),
                               ("mount", False), ("action", False)):
            self.check(row.get(flag) == str(expected).lower(), f"Dedicated module {flag}={expected}")
        if not update:
            self.check(row.get("web") == str(self.state["with_webui"]).lower(),
                       "Dedicated module WebUI resource flag matches the fixture")

    def verify_files(self, root):
        _, files, _ = fixture(self.state["test_id"], self.state["with_webui"])
        for name, content in files.items():
            path = root + "/" + name
            self.shell(f"[ -f {shlex.quote(path)} ] && [ ! -L {shlex.quote(path)} ]")
            result = self.command(["sha256sum", path]).split()
            self.check(bool(result) and result[0] == digest(content),
                       "Dedicated fixture file digest matches: " + name)

    def no_staging(self):
        path = shlex.quote(self.staged)
        self.shell(f"[ ! -e {path} ] && [ ! -L {path} ]")
        self.check(True, "Dedicated test module has no staged update")

    def prepare(self, boot):
        self.check(not self.args.state.exists(), "Prepare requires a fresh local state path")
        test_id = uuid.uuid4().hex
        self.use_state({
            "schema": SCHEMA, "test_id": test_id, "serial": self.args.serial,
            "ksud": self.args.ksud, "with_webui": self.args.with_webui,
            "status": "preparing", "prepare_boot_id": boot,
        })
        rows = self.modules()
        self.check(self.module_id not in rows, "Fresh test module ID is absent")
        self.check(all(row["update"] == "false" and row["remove"] == "false"
                       for row in rows.values()),
                   "Original modules have no pending updates or removals")
        # A reboot must not apply unrelated staged modules absent from module list.
        self.shell('for p in /data/adb/modules_update/* /data/adb/modules_update/.[!.]* '
                   '/data/adb/modules_update/..?*; do '
                   '[ ! -e "$p" ] && [ ! -L "$p" ] || exit 1; done')
        self.state["baseline"] = self.snapshot(rows)
        self.state["baseline_disk"] = self.disk_snapshot()
        self.persist()  # Persist the UUID and baseline before any remote mutation.
        archive, _, _ = fixture(test_id, self.args.with_webui)
        local_zip = self.args.state.parent / (self.module_id + ".zip")
        with local_zip.open("xb") as output:
            output.write(archive)
        self.state.update(archive_path=str(local_zip.resolve()), archive_sha256=digest(archive))
        self.persist()
        guards = [
            f"([ ! -e {shlex.quote(p)} ] && [ ! -L {shlex.quote(p)} ]) || exit 1"
            for p in (self.directory, self.active, self.staged)
        ]
        parents = ("/data/adb", "/data/adb/ksu", "/data/adb/ksu/XKCNSU", BASE)
        guards += [f"[ ! -L {shlex.quote(p)} ] || exit 1" for p in parents]
        self.shell("set -eu; umask 077; " + "; ".join(guards)
                   + f"; mkdir -p {shlex.quote(BASE)}; mkdir {shlex.quote(self.directory)}")
        # ASCII avoids Windows adb shell truncation on binary control bytes.
        directory = shlex.quote(self.directory)
        remote_zip = shlex.quote(self.remote_zip)
        self.shell(
            f"set -eu; umask 077; set -C; "
            f"([ -d {directory} ] && [ ! -L {directory} ]) || exit 1; "
            f"([ ! -e {remote_zip} ] && [ ! -L {remote_zip} ]) || exit 1; "
            f"base64 -d > {remote_zip}",
            payload=base64.b64encode(archive) + b"\n",
        )
        uploaded = self.command(["sha256sum", self.remote_zip]).split()
        self.check(bool(uploaded) and uploaded[0] == digest(archive), "Uploaded ZIP digest matches")
        self.daemon("install", self.remote_zip, timeout=self.args.install_timeout)
        rows = self.modules()
        self.own_row(rows, update=True)
        self.unchanged(rows)
        self.verify_files(self.staged)
        self.daemon("disable", self.module_id)
        rows = self.modules()
        self.own_row(rows, update=True, enabled=False)
        self.unchanged(rows)
        self.daemon("enable", self.module_id)
        rows = self.modules()
        self.own_row(rows, update=True)
        self.unchanged(rows)
        for stage in ("post-fs-data", "service"):
            path = shlex.quote(self.directory + "/" + stage)
            self.shell(f"[ ! -e {path} ] && [ ! -L {path} ]")
        self.check(self.boot_id() == boot, "Prepare stayed within one kernel boot")
        self.state["status"] = "prepared"
        self.persist()
        self.report["reboot_required"] = True

    def after_boot(self, boot):
        self.check(self.state["status"] in ("prepared", "boot_verified"),
                   "After-boot requires a completed prepare")
        self.check(boot != self.state["prepare_boot_id"], "A kernel reboot occurred after prepare")
        rows = self.modules()
        self.own_row(rows)
        self.unchanged(rows)
        self.no_staging()
        self.verify_files(self.active)
        deadline = time.monotonic() + self.args.marker_timeout
        for stage in ("post-fs-data", "service"):
            path = shlex.quote(self.directory + "/" + stage)
            expected = self.state["test_id"] + ":" + stage + ":" + boot
            while True:
                observed = self.shell(
                    f"if [ -f {path} ] && [ ! -L {path} ]; then head -c 256 {path}; fi")
                if observed == expected or time.monotonic() >= deadline:
                    break
                time.sleep(min(1, max(0, deadline - time.monotonic())))
            self.check(observed == expected, stage + " marker matches this test and kernel boot")
        self.check(self.boot_id() == boot, "After-boot stayed within one kernel boot")
        self.state.update(status="boot_verified", verified_boot_id=boot)
        self.persist()

    def cleanup(self, boot):
        rows = self.modules()
        self.unchanged(rows)
        if (self.state["status"] == "uninstall_pending"
                and not self.state.get("uninstall_command_verified")):
            self.check(boot == self.state["uninstall_boot_id"],
                       "Retry an uncertain uninstall before another reboot")
            self.check(self.module_id in rows, "Uncertain uninstall still has a dedicated module")
            self.check(rows[self.module_id]["remove"] in ("true", "false"),
                       "Uncertain uninstall has a readable removal flag")
            self.own_row(rows, remove=rows[self.module_id]["remove"] == "true")
            self.no_staging()
            self.daemon("uninstall", self.module_id)
            rows = self.modules()
            self.own_row(rows, remove=True)
            self.unchanged(rows)
            self.check(self.boot_id() == boot, "Retried uninstall stayed within one kernel boot")
            self.state["uninstall_command_verified"] = True
            self.persist()
            self.report["reboot_required"] = True
            return
        if self.state["status"] in ("uninstall_pending", "cleaned"):
            self.report["reboot_required"] = boot == self.state["uninstall_boot_id"]
            self.check(boot != self.state["uninstall_boot_id"],
                       "A second kernel reboot occurred after formal uninstall")
            self.check(self.state.get("uninstall_command_verified") is True,
                       "Formal uninstall command and removal flag were verified")
            self.check(self.module_id not in rows, "Only the dedicated module disappeared from list")
            for root in (self.active, self.staged):
                path = shlex.quote(root)
                self.shell(f"[ ! -e {path} ] && [ ! -L {path} ]")
            self.check(self.boot_id() == boot, "Cleanup verification stayed within one kernel boot")
            self.state.update(status="cleaned", cleaned_boot_id=boot)
            self.persist()
            self.report["cleanup_verified"] = True
            # Preserve the UUID markers, ZIP and local evidence for inspection.
            return
        self.check(self.state["status"] in ("preparing", "prepared", "boot_verified"),
                   "Cleanup state is eligible for formal uninstall")
        self.check(boot != self.state["prepare_boot_id"],
                   "Apply the staged install by rebooting before uninstall")
        self.own_row(rows)
        self.no_staging()
        # Journal before invoking a mutating command: timeouts may still mutate.
        self.state.update(status="uninstall_pending", uninstall_boot_id=boot)
        self.persist()
        self.daemon("uninstall", self.module_id)
        rows = self.modules()
        self.own_row(rows, remove=True)
        self.unchanged(rows)
        self.check(self.boot_id() == boot, "Formal uninstall stayed within one kernel boot")
        self.state["uninstall_command_verified"] = True
        self.persist()
        self.report["reboot_required"] = True

    def run(self):
        try:
            self.check(self.args.execute, "Device access requires explicit --execute")
            self.check(bool(self.args.serial.strip()) and "\x00" not in self.args.serial,
                       "An explicit nonempty device serial is required")
            self.check(self.args.ksud.startswith("/") and "\x00" not in self.args.ksud
                       and "\n" not in self.args.ksud and "\r" not in self.args.ksud,
                       "ksud is an explicit absolute Android path")
            self.check(self.args.state.resolve() != self.args.report.resolve(),
                       "State and report paths differ")
            if self.args.phase != "prepare":
                self.load()
            self.check(self.command(["id", "-u"]) == "0", "Root shell works")
            self.check(self.command(["getprop", "sys.boot_completed"]) == "1",
                       "Android boot completed")
            boot = self.boot_id()
            if self.args.phase == "prepare":
                self.prepare(boot)
            elif self.args.phase == "after-boot":
                self.after_boot(boot)
            else:
                self.cleanup(boot)
            self.report["completed"] = True
        except Exception as error:
            self.report["error"] = str(error)
            self.report["error_type"] = type(error).__name__
        finally:
            self.report["finished_unix"] = time.time()
            save_json(self.args.report, self.report)
        # No inventory, command output or exception traceback goes to the terminal.
        print(json.dumps({
            key: self.report.get(key) for key in (
                "phase", "test_id", "module_id", "state", "completed",
                "state_status", "reboot_required", "cleanup_verified", "error")
        }, ensure_ascii=True))
        return 0 if self.report["completed"] else 1


def positive_seconds(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("Timeout must be finite and positive")
    return value


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--ksud", required=True)
    parser.add_argument("--phase", choices=("prepare", "after-boot", "cleanup"), required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--with-webui", action="store_true",
                        help="Prepare a static WebUI resource; subsequent phases use saved state")
    parser.add_argument("--timeout", type=positive_seconds, default=45)
    parser.add_argument("--install-timeout", type=positive_seconds, default=120)
    parser.add_argument("--marker-timeout", type=positive_seconds, default=60)
    args = parser.parse_args(argv)
    if args.state.resolve() == args.report.resolve():
        parser.error("--state and --report must be different files")
    if args.with_webui and args.phase != "prepare":
        parser.error("--with-webui is only valid for prepare")
    return args


def self_test():
    """No adb process, build, or device required."""
    import contextlib
    import unittest

    class LocalTests(unittest.TestCase):
        def setUp(self):
            self.test_id = "123456781234423489ab123456789abc"
            self.module_id, self.directory = identity(self.test_id)
            self.temporary = tempfile.TemporaryDirectory()
            self.addCleanup(self.temporary.cleanup)
            root = Path(self.temporary.name)
            self.args = parse_args([
                "--serial", "fake serial; $x", "--ksud", "/data/adb/ksud's copy",
                "--phase", "prepare", "--state", str(root / "state.json"),
                "--report", str(root / "report.json"), "--execute",
            ])
            self.case = Acceptance(self.args)
            self.case.use_state({
                "schema": SCHEMA, "test_id": self.test_id, "serial": self.args.serial,
                "ksud": self.args.ksud, "with_webui": False, "status": "prepared",
                "prepare_boot_id": "old-boot", "baseline": {}, "baseline_disk": {},
            })

        def row(self):
            _, _, props = fixture(self.test_id)
            return dict(props, enabled="true", update="false", remove="false",
                        web="false", action="false", mount="false")

        def test_zip_contents_and_namespace(self):
            archive, files, _ = fixture(self.test_id, True)
            with zipfile.ZipFile(io.BytesIO(archive)) as source:
                self.assertEqual(set(source.namelist()), {
                    "module.prop", "skip_mount", "post-fs-data.sh", "service.sh",
                    "webroot/index.html",
                })
                self.assertEqual(source.testzip(), None)
                for name, value in files.items():
                    self.assertEqual(source.read(name), value)
            for stage in ("post-fs-data", "service"):
                script = files[stage + ".sh"].decode()
                self.assertIn(self.directory, script)
                self.assertIn(self.test_id + ":" + stage + ":", script)
                self.assertNotIn("rm ", script)
                self.assertNotIn("reboot", script)
            for bad in ("../escape", "a" * 32, self.test_id.upper()):
                with self.assertRaises(ValueError):
                    identity(bad)

        def test_shell_boundaries(self):
            observed = []

            def fake(argv, **kwargs):
                observed.append((argv, kwargs))
                return subprocess.CompletedProcess(argv, 0, b"ok\n", b"")

            self.case.runner = fake
            self.case.daemon("install", "/a b/quote's;$x.zip")
            argv, options = observed[0]
            self.assertEqual(argv[:5], ["adb", "-s", self.args.serial, "shell", "-T"])
            outer = shlex.split(argv[5])
            self.assertEqual(outer[:2], ["su", "-c"])
            self.assertEqual(shlex.split(outer[2]), [
                self.args.ksud, "module", "install", "/a b/quote's;$x.zip"])
            self.assertEqual(options["timeout"], 45)

        def test_no_execute_never_invokes_transport(self):
            self.args.execute = False
            self.case.runner = lambda *a, **k: self.fail("Transport invoked without --execute")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.case.run(), 1)
            report = json.loads(self.args.report.read_text())
            self.assertFalse(report["completed"])
            self.assertEqual(report["commands"], [])

        def test_timeout_preserves_partial_output(self):
            def fake(argv, **kwargs):
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"], b"partial", b"failure")
            self.case.runner = fake
            with self.assertRaises(RuntimeError):
                self.case.command(["id"])
            entry = self.case.report["commands"][0]
            self.assertTrue(entry["timed_out"])
            self.assertIsNone(entry["exit_code"])
            self.assertEqual(entry["stdout"], "partial")
            self.assertEqual(entry["stderr"], "failure")

        def test_inventory_and_nonzero_results(self):
            def fake(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 7, b"private module data", b"real error")
            self.case.runner = fake
            with self.assertRaises(RuntimeError):
                self.case.modules()
            entry = self.case.report["commands"][0]
            self.assertEqual(entry["exit_code"], 7)
            self.assertEqual(entry["stdout_sha256"], digest(b"private module data"))
            self.assertNotIn("stdout", entry)
            original = dict(self.row(), id="original", description="private description")
            self.case.state["baseline"] = self.case.snapshot({"original": original})
            self.case.disk_snapshot = lambda: {}
            self.case.unchanged({"original": dict(original)})
            for key, value in (("enabled", "false"), ("version", "changed"),
                               ("description", "changed")):
                with self.assertRaises(RuntimeError):
                    self.case.unchanged({"original": dict(original, **{key: value})})
            with self.assertRaises(RuntimeError):
                self.case.unchanged({})

        def test_reboot_guards_and_cleanup_verification(self):
            self.case.modules = lambda: {self.module_id: self.row()}
            self.case.disk_snapshot = lambda: {}
            with self.assertRaises(RuntimeError):
                self.case.after_boot("old-boot")
            with self.assertRaises(RuntimeError):
                self.case.cleanup("old-boot")
            self.case.state.update(status="uninstall_pending", uninstall_boot_id="new-boot",
                                   uninstall_command_verified=True)
            with self.assertRaises(RuntimeError):
                self.case.cleanup("new-boot")
            with self.assertRaises(RuntimeError):
                self.case.cleanup("third-boot")
            self.case.modules = lambda: {}
            self.case.shell = lambda *a, **k: ""
            self.case.boot_id = lambda: "third-boot"
            self.case.cleanup("third-boot")
            self.assertTrue(self.case.report["cleanup_verified"])
            self.assertEqual(self.case.state["status"], "cleaned")

        def test_stale_or_missing_markers_fail(self):
            self.case.modules = lambda: {self.module_id: self.row()}
            self.case.no_staging = lambda: None
            self.case.verify_files = lambda root: None
            self.case.disk_snapshot = lambda: {}
            self.case.shell = lambda *a, **k: self.test_id + ":service:old-boot"
            self.args.marker_timeout = 0
            with self.assertRaises(RuntimeError):
                self.case.after_boot("new-boot")
            self.assertEqual(self.case.state["status"], "prepared")

        def test_full_lifecycle_fake_transport(self):
            boot = "11111111-1111-4111-8111-111111111111"
            installed = False
            pending = False
            enabled = True
            removed = False
            uploaded_archive = None
            operations = []
            original = dict(self.row(), id="original", description="user private metadata")
            self.args.with_webui = True

            def fake(argv, **kwargs):
                nonlocal installed, enabled, pending, removed, uploaded_archive
                arguments = shlex.split(shlex.split(argv[-1])[2])
                output = ""
                if arguments == ["id", "-u"]:
                    output = "0"
                elif arguments == ["getprop", "sys.boot_completed"]:
                    output = "1"
                elif arguments == ["cat", "/proc/sys/kernel/random/boot_id"]:
                    output = boot
                elif arguments[0] == self.args.ksud:
                    operation = arguments[2]
                    operations.append(operation)
                    if operation == "list":
                        rows = [original]
                        if installed:
                            _, _, props = fixture(self.case.state["test_id"], True)
                            rows.append(dict(props, enabled=str(enabled).lower(),
                                             update=str(pending).lower(),
                                             remove=str(removed).lower(), web=str(not pending).lower(),
                                             action="false", mount="false"))
                        output = json.dumps(rows)
                    elif operation == "install":
                        self.assertEqual(arguments[3], self.case.remote_zip)
                        installed, pending = True, True
                    else:
                        self.assertEqual(arguments[3], self.case.module_id)
                        if operation == "disable":
                            enabled = False
                        elif operation == "enable":
                            enabled = True
                        elif operation == "uninstall":
                            removed = True
                        else:
                            self.fail("Unexpected module operation: " + operation)
                elif arguments[0] == "sha256sum":
                    archive, files, _ = fixture(self.case.state["test_id"], True)
                    path = arguments[1]
                    if path == self.case.remote_zip:
                        self.assertIsNotNone(uploaded_archive)
                        value = uploaded_archive
                    else:
                        value = files[path.split(self.case.module_id + "/", 1)[1]]
                    output = digest(value) + "  " + path
                elif arguments[:2] == ["sh", "-c"]:
                    script = arguments[2]
                    self.assertNotIn("rm ", script)
                    self.assertNotIn("reboot", script)
                    if kwargs["input"] is not None:
                        self.assertTrue(script.endswith(
                            "base64 -d > " + shlex.quote(self.case.remote_zip)))
                        self.assertIn("set -C;", script)
                        self.assertIn("[ ! -L " + shlex.quote(self.case.directory) + " ]", script)
                        self.assertIn("[ ! -e " + shlex.quote(self.case.remote_zip) + " ]", script)
                        self.assertIn("[ ! -L " + shlex.quote(self.case.remote_zip) + " ]", script)
                        payload = kwargs["input"]
                        self.assertTrue(payload.endswith(b"\n"))
                        self.assertTrue(all(32 <= byte <= 126 for byte in payload[:-1]))
                        uploaded_archive = base64.b64decode(payload[:-1], validate=True)
                        self.assertEqual(uploaded_archive, fixture(
                            self.case.state["test_id"], True)[0])
                    if "head -c 256" in script:
                        stage = "post-fs-data" if "/post-fs-data" in script else "service"
                        output = self.case.state["test_id"] + ":" + stage + ":" + boot
                    if script.startswith("# original-module-fingerprint"):
                        output = digest("original") + " " + digest("original module.prop") + " 000"
                else:
                    self.fail("Unexpected transport command: " + repr(arguments))
                return subprocess.CompletedProcess(argv, 0, output.encode(), b"")

            def run_phase():
                self.case = Acceptance(self.args, runner=fake)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(self.case.run(), 0, self.case.report.get("error"))

            run_phase()
            self.assertEqual(self.case.state["status"], "prepared")
            self.assertEqual([op for op in operations if op != "list"],
                             ["install", "disable", "enable"])
            self.assertNotIn("user private metadata", self.args.report.read_text())
            boot = "22222222-2222-4222-8222-222222222222"
            pending = False
            self.args.phase = "after-boot"
            self.args.with_webui = False
            run_phase()
            self.assertEqual(self.case.state["status"], "boot_verified")
            self.args.phase = "cleanup"
            run_phase()
            self.assertEqual(self.case.state["status"], "uninstall_pending")
            self.assertFalse(self.case.report["cleanup_verified"])
            self.assertTrue(self.case.report["reboot_required"])
            boot = "33333333-3333-4333-8333-333333333333"
            installed = False
            run_phase()
            self.assertEqual(self.case.state["status"], "cleaned")
            self.assertTrue(self.case.report["cleanup_verified"])
            self.assertEqual(operations.count("uninstall"), 1)

        def test_raw_metadata_changes_are_not_hidden_by_cli_overrides(self):
            self.case.disk_snapshot = lambda: {digest("original"): digest("changed") + " 000"}
            self.case.state["baseline_disk"] = {
                digest("original"): digest("original") + " 000"}
            with self.assertRaises(RuntimeError):
                self.case.unchanged({})

        def test_uncertain_uninstall_cannot_pass_after_reboot(self):
            self.case.state.update(status="uninstall_pending", uninstall_boot_id="new-boot")
            self.case.modules = lambda: {}
            self.case.disk_snapshot = lambda: {}
            with self.assertRaises(RuntimeError):
                self.case.cleanup("third-boot")
            self.assertFalse(self.case.report["cleanup_verified"])

        def test_state_report_collision_is_rejected_before_execution(self):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parse_args(["--serial", "fake", "--ksud", "/ksud", "--phase", "prepare",
                                "--state", "same.json", "--report", "same.json", "--execute"])

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(LocalTests)
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--self-test"]:
        return self_test()
    return Acceptance(parse_args(argv)).run()


if __name__ == "__main__":
    sys.exit(main())
