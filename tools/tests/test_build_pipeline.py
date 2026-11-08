#!/usr/bin/env python3
"""Build-pipeline regression tests using temporary assets and stubbed build tools."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


TOOLS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("build_verify", TOOLS / "build-verify.py")
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)
RELEASE_SOURCE = (TOOLS / "build-release.sh").read_text()
ASSET_PATHS = {
    "ksuinit": "ksuinit/arm64-v8a/ksuinit",
    "kpimg": "kpatch/aarch64/kpimg",
    "kptools": "kpatch/aarch64/kptools",
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def elf(label):
    data = bytearray(b"\x7fELF" + b"\0" * 20)
    data[4:6] = b"\x02\x01"
    data[18:20] = (183).to_bytes(2, "little")
    return bytes(data) + label.encode()


class TemporaryAssets(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="xkcnsu pipeline tests ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.out = self.root / "out"

    def put(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def put_json(self, relative, value):
        return self.put(relative, json.dumps(value).encode())

    def read_json(self, relative):
        return json.loads((self.root / relative).read_bytes())


class BuildVerifyTests(TemporaryAssets):
    def setUp(self):
        super().setUp()
        self.certificate_hash = "ab" * 32
        self.put("version.properties",
                 b"XKCNSU_VERSION_NAME=0.2.0\nXKCNSU_VERSION_CODE=32800\n")
        self.put("kernel/manager/apk_sign.c", b"#define CERT_MAX_LENGTH 512\n")
        self.put_json("out/private/certificate.json", {
            "certificate_size": 256,
            "KSU_EXPECTED_HASH": self.certificate_hash,
            "KSU_PACKAGE_NAME": "io.xkcnsu.manager",
        })
        self.assets = {name: elf("fresh-" + name) for name in ASSET_PATHS}
        for name, relative in ASSET_PATHS.items():
            self.put("out/" + relative, self.assets[name])
        self.modules = {
            "android14-6.1_kernelsu.ko": elf("root"),
            "android14-6.1_susfs_guard_lkm.ko": elf("guard"),
        }
        for name, data in self.modules.items():
            self.put("out/aarch64/" + name, data)
        self.embedded = [
            {"name": name, "size": len(data), "sha256": sha(data)}
            for name, data in (self.assets | self.modules).items()
        ]
        self.write_embedded(self.embedded)
        self.daemon = elf("release-daemon")
        self.put("out/ksud/arm64-v8a/ksud", self.daemon)
        self.apk = self.root / "manager/fixture.apk"
        self.write_apk(self.daemon)
        self.final_manifest = self.out / "manifests/final-apk.json"
        self.module_result = subprocess.CompletedProcess([], 0)
        self.signing = "Signer #1 certificate SHA-256 digest: " + self.certificate_hash
        self.badging = (
            "package: name='io.xkcnsu.manager' versionCode='32800' "
            "versionName='0.2.0'\n"
        )
        self.manifest_xml = "E: manifest\n  E: application\n"
        self.environment = {
            "XKCNSU_ROOT": str(self.root),
            "XKCNSU_OUT": str(self.out),
            "XKCNSU_REQUIRE_FULL_MATRIX": "1",
        }
        self.module_run = mock.Mock(side_effect=self.run_modules)
        self.android_output = mock.Mock(side_effect=self.android_tool)

    def write_embedded(self, assets):
        self.put_json("out/manifests/embedded-assets-aarch64.json", assets)

    def add_x86_daemon(self, machine=62):
        daemon = bytearray(elf("x86-daemon"))
        daemon[18:20] = machine.to_bytes(2, "little")
        self.put("out/ksud/x86_64/ksud", daemon)
        with zipfile.ZipFile(self.apk, "a") as archive:
            archive.writestr("lib/x86_64/libksud.so", daemon)
            for library in ("libkernelsu.so", "libadbroot.so"):
                archive.writestr(f"lib/x86_64/{library}", daemon)
        return bytes(daemon)

    def test_verifies_every_packaged_daemon_abi(self):
        daemon = self.add_x86_daemon()
        self.environment["XKCNSU_ABIS"] = "arm64-v8a x86_64"
        self.verify()
        self.assertEqual(self.read_json("out/manifests/final-apk.json")["native_daemons"],
                         {"arm64-v8a": sha(self.daemon), "x86_64": sha(daemon)})

    def test_rejects_missing_requested_abi(self):
        self.environment["XKCNSU_ABIS"] = "arm64-v8a x86_64"
        with self.assertRaisesRegex(SystemExit, "daemon ABIs differ"):
            self.verify()

    def test_rejects_wrong_machine_in_x86_daemon(self):
        self.add_x86_daemon(machine=183)
        with self.assertRaisesRegex(SystemExit, "wrong ELF machine"):
            self.verify()

    def test_rejects_stale_x86_daemon(self):
        self.add_x86_daemon()
        self.put("out/ksud/x86_64/ksud", elf("stale"))
        with self.assertRaisesRegex(SystemExit, "release daemon: x86_64"):
            self.verify()

    def test_rejects_missing_x86_bridge_even_when_daemon_is_present(self):
        self.add_x86_daemon()
        with zipfile.ZipFile(self.apk) as archive:
            entries = [(item.filename, archive.read(item)) for item in archive.infolist()
                       if item.filename != "lib/x86_64/libkernelsu.so"]
        with zipfile.ZipFile(self.apk, "w") as archive:
            for name, data in entries:
                archive.writestr(name, data)
        with self.assertRaisesRegex(SystemExit, "missing a native library"):
            self.verify()

    def write_apk(self, daemon):
        self.apk.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(self.apk, "w") as archive:
            archive.writestr("lib/arm64-v8a/libksud.so", daemon)
            for library in ("libkernelsu.so", "libadbroot.so"):
                archive.writestr(f"lib/arm64-v8a/{library}", self.daemon)
            archive.writestr("AndroidManifest.xml", b"synthetic-manifest")

    def run_modules(self, arguments, **kwargs):
        expected = [sys.executable, str(self.root / "tools/build-verify-modules.py")]
        if self.environment["XKCNSU_REQUIRE_FULL_MATRIX"] != "1":
            expected += ["--kmi", "android14-6.1", "--kmi", "android12-5.10"]
        self.assertEqual(arguments, expected)
        self.assertEqual(kwargs, {"check": False})
        return self.module_result

    def android_tool(self, arguments, **kwargs):
        self.assertEqual(kwargs, {"text": True})
        responses = {
            ("apksigner", "verify", "--print-certs", str(self.apk)): self.signing,
            ("aapt2", "dump", "badging", str(self.apk)): self.badging,
            ("aapt2", "dump", "xmltree", "--file",
             "AndroidManifest.xml", str(self.apk)): self.manifest_xml,
        }
        self.assertIn(tuple(arguments), responses, "Unexpected external command")
        return responses[tuple(arguments)]

    def verify(self):
        with mock.patch.dict(os.environ, self.environment, clear=True), \
                mock.patch.object(sys, "argv", ["build-verify.py", str(self.apk)]), \
                mock.patch.object(VERIFY.subprocess, "run", self.module_run), \
                mock.patch.object(VERIFY.subprocess, "check_output", self.android_output), \
                contextlib.redirect_stdout(io.StringIO()):
            VERIFY.main()

    def assert_asset_rejected(self, name):
        with self.assertRaisesRegex(SystemExit, "embedded asset is stale: " + name):
            self.verify()
        self.module_run.assert_called_once()
        self.android_output.assert_not_called()
        self.assertFalse(self.final_manifest.exists())
        self.module_run.reset_mock()

    def test_matching_assets_publish_verified_apk_identity(self):
        self.verify()
        self.module_run.assert_called_once()
        self.assertEqual(self.android_output.call_count, 3)
        self.assertEqual(self.read_json("out/manifests/final-apk.json"), {
            "apk": str(self.apk),
            "sha256": sha(self.apk.read_bytes()),
            "size": self.apk.stat().st_size,
            "package_name": "io.xkcnsu.manager",
            "version_name": "0.2.0",
            "version_code": 32800,
            "certificate_sha256": self.certificate_hash,
            "ksud_sha256": sha(self.daemon),
            "native_daemons": {"arm64-v8a": sha(self.daemon)},
        })

    def test_partial_matrix_option_still_checks_embedded_assets(self):
        self.environment["XKCNSU_REQUIRE_FULL_MATRIX"] = "0"
        self.put("out/" + ASSET_PATHS["kptools"], elf("stale-tool"))
        self.assert_asset_rejected("kptools")

    def test_manager_update_can_keep_the_bundled_kernel_version(self):
        self.put("version.properties",
                 b"XKCNSU_VERSION_NAME=0.2.1\nXKCNSU_VERSION_CODE=32800\n"
                 b"XKCNSU_MANAGER_VERSION_CODE=32801\n")
        self.badging = self.badging.replace("32800", "32801").replace("0.2.0", "0.2.1")
        self.verify()
        manifest = self.read_json("out/manifests/final-apk.json")
        self.assertEqual(manifest["version_name"], "0.2.1")
        self.assertEqual(manifest["version_code"], 32801)

    def test_formal_release_rejects_undeclared_or_development_signing(self):
        self.environment["XKCNSU_REQUIRE_RELEASE_SIGNING"] = "1"
        for profile, development in ((None, None), ("development", True),
                                     ("release", True), ("release", "false")):
            with self.subTest(profile=profile, development=development):
                signing = self.read_json("out/private/certificate.json")
                signing.update(signing_profile=profile, development_only=development)
                self.put_json("out/private/certificate.json", signing)
                with self.assertRaisesRegex(SystemExit, "release signing is required"):
                    self.verify()
                self.module_run.assert_not_called()
                self.android_output.assert_not_called()
                self.assertFalse(self.final_manifest.exists())

    def test_formal_release_records_verified_signing_profile(self):
        self.environment["XKCNSU_REQUIRE_RELEASE_SIGNING"] = "1"
        signing = self.read_json("out/private/certificate.json")
        signing.update(signing_profile="release", development_only=False)
        self.put_json("out/private/certificate.json", signing)
        self.verify()
        final = self.read_json("out/manifests/final-apk.json")
        self.assertEqual(final["signing_profile"], "release")
        self.assertIs(final["development_only"], False)
        self.assertEqual(final["certificate_sha256"], self.certificate_hash)

    def test_debuggable_and_test_only_manifest_attributes_block_publication(self):
        for attribute in ("debuggable", "testOnly"):
            for value in ("true", "(type 0x12)0xffffffff"):
                with self.subTest(attribute=attribute, value=value):
                    self.manifest_xml = "E: manifest\n  A: android:" + attribute + "=" + value + "\n"
                    with self.assertRaisesRegex(SystemExit, "must"):
                        self.verify()
                    self.assertFalse(self.final_manifest.exists())

    def test_false_debuggable_and_test_only_attributes_allow_publication(self):
        self.manifest_xml = (
            "E: manifest\n"
            "  A: android:debuggable=(type 0x12)0x0\n"
            "  A: android:testOnly=(type 0x12)0x0\n"
        )
        self.verify()
        self.assertTrue(self.final_manifest.is_file())

    def test_each_changed_build_output_blocks_before_signer(self):
        for name, relative in ASSET_PATHS.items():
            with self.subTest(asset=name):
                self.put("out/" + relative, elf("new-build-" + name))
                self.assert_asset_rejected(name)
                self.put("out/" + relative, self.assets[name])

    def test_each_missing_build_output_blocks_before_signer(self):
        for name, relative in ASSET_PATHS.items():
            with self.subTest(asset=name):
                (self.out / relative).unlink()
                self.assert_asset_rejected(name)
                self.put("out/" + relative, self.assets[name])

    def test_directory_cannot_replace_a_build_output(self):
        for name, relative in ASSET_PATHS.items():
            with self.subTest(asset=name):
                path = self.out / relative
                path.unlink()
                path.mkdir()
                self.assert_asset_rejected(name)
                path.rmdir()
                self.put("out/" + relative, self.assets[name])

    def test_each_empty_build_output_blocks_before_signer(self):
        for name, relative in ASSET_PATHS.items():
            with self.subTest(asset=name):
                self.put("out/" + relative, b"")
                self.assert_asset_rejected(name)
                self.put("out/" + relative, self.assets[name])

    def test_each_asset_missing_from_embedded_manifest_blocks(self):
        for name in ASSET_PATHS:
            with self.subTest(asset=name):
                self.write_embedded([item for item in self.embedded if item["name"] != name])
                self.assert_asset_rejected(name)

    def test_each_stale_embedded_hash_blocks_even_when_output_is_fresh(self):
        for name in ASSET_PATHS:
            with self.subTest(asset=name):
                assets = [dict(item) for item in self.embedded]
                next(item for item in assets if item["name"] == name)["sha256"] = sha(b"old")
                self.write_embedded(assets)
                self.assert_asset_rejected(name)

    def test_native_checks_do_not_replace_existing_module_hash_check(self):
        name = "android14-6.1_kernelsu.ko"
        self.put("out/aarch64/" + name, elf("new-root"))
        self.assert_asset_rejected(name)

    def test_module_failure_stops_native_and_signer_validation(self):
        self.module_result.returncode = 1
        (self.out / ASSET_PATHS["ksuinit"]).unlink()
        with self.assertRaisesRegex(SystemExit, "KMI module set is incomplete"):
            self.verify()
        self.android_output.assert_not_called()
        self.assertFalse(self.final_manifest.exists())

    def test_valid_native_inputs_do_not_bypass_apk_signer(self):
        self.signing = "Signer #1 certificate SHA-256 digest: " + "cd" * 32
        with self.assertRaisesRegex(SystemExit, "signing certificate does not match"):
            self.verify()
        self.assertEqual(self.android_output.call_count, 1)
        self.assertFalse(self.final_manifest.exists())

    def test_valid_native_inputs_do_not_bypass_release_version(self):
        self.badging = self.badging.replace("32800", "32716")
        with self.assertRaisesRegex(SystemExit, "release version does not match"):
            self.verify()
        self.assertEqual(self.android_output.call_count, 2)
        self.assertFalse(self.final_manifest.exists())

    def test_valid_native_inputs_do_not_allow_a_stale_apk_daemon(self):
        self.write_apk(elf("old-apk-daemon"))
        with self.assertRaisesRegex(SystemExit, "libksud.so differs"):
            self.verify()
        self.assertFalse(self.final_manifest.exists())

    def test_matching_non_arm64_daemon_is_still_rejected(self):
        daemon = bytearray(self.daemon)
        daemon[18:20] = (62).to_bytes(2, "little")
        self.put("out/ksud/arm64-v8a/ksud", daemon)
        self.write_apk(daemon)
        with self.assertRaisesRegex(SystemExit, "wrong ELF machine for arm64-v8a"):
            self.verify()
        self.assertFalse(self.final_manifest.exists())


RELEASE_STUBS = r"""
xkcnsu_freeze() { :; }
xkcnsu_log() { :; }
xkcnsu_space() { :; }
xkcnsu_heavy_lock() { :; }
xkcnsu_die() { printf '%s\n' "$*" >&2; exit 1; }
xkcnsu_run() {
    printf '%s\n' "$*" >>"$XKCNSU_TEST_COMMAND_LOG"
    if [[ "$1" == python3 && "$2" == "$XKCNSU_ROOT/tools/build-verify-modules.py" ]]; then
        return
    fi
    case "$*" in
        "cargo clean --package ksud --target $triple --release") ;;
        "cargo build --locked --package ksud --target $triple --release")
            mkdir -p "$XKCNSU_TEST_CAPTURE/$arch"
            cp -a "$work/userspace/ksud/bin/$arch/." "$XKCNSU_TEST_CAPTURE/$arch/"
            cp "$XKCNSU_OUT/manifests/embedded-assets-$arch.json" "$XKCNSU_TEST_CAPTURE/$arch.json"
            ;;
        *) xkcnsu_die "Unexpected external command: $*" ;;
    esac
}
"""


@unittest.skipUnless(
    all(shutil.which(command) for command in ("bash", "flock", "sha256sum", "python3")),
    "Requires Bash, flock, sha256sum and Python3",
)
class ReleaseKsudTests(TemporaryAssets):
    def setUp(self):
        super().setUp()
        self.work = self.root / "release source"
        (self.work / ".git").mkdir(parents=True)
        self.put("tools/build-release.sh", RELEASE_SOURCE.encode())
        self.put("tools/build-common.sh", RELEASE_STUBS.encode())
        self.put("release source/.github/scripts/setup-rust-build.sh", b":\n")
        self.arches = {
            "arm64-v8a": ("aarch64", "aarch64-linux-android"),
            "x86_64": ("x86_64", "x86_64-linux-android"),
        }
        self.fresh = {}
        self.old_init = b"stale-snapshot-init"
        for abi, (arch, triple) in self.arches.items():
            self.fresh[abi] = elf("fresh-init-" + abi)
            self.put(f"out/ksuinit/{abi}/ksuinit", self.fresh[abi])
            self.put(f"release source/userspace/ksud/bin/{arch}/ksuinit", self.old_init)
            for suffix in ("kernelsu", "susfs_guard_lkm"):
                self.put(f"out/{arch}/android14-6.1_{suffix}.ko",
                         elf(arch + "-" + suffix))
            self.put(f"target/{triple}/release/ksud", elf("built-daemon-" + abi))
        self.tools = {"kpimg": b"fresh-engine-image", "kptools": elf("fresh-tools")}
        for name, data in self.tools.items():
            self.put("out/kpatch/aarch64/" + name, data)
            self.put("release source/userspace/ksud/bin/aarch64/" + name, b"stale-" + name.encode())
        self.command_log = self.root / "commands.log"
        self.capture = self.root / "compile-inputs"
        self.environment = os.environ.copy()
        self.environment.update({
            "XKCNSU_ROOT": str(self.root),
            "XKCNSU_OUT": str(self.out),
            "XKCNSU_CACHE": str(self.root / "cache"),
            "XKCNSU_RELEASE_SOURCE": str(self.work),
            "XKCNSU_ABIS": "arm64-v8a",
            "XKCNSU_PACKAGE_NAME": "io.xkcnsu.manager",
            "XKCNSU_ANDROID_API": "29",
            "XKCNSU_REQUIRE_FULL_MATRIX": "1",
            "XKCNSU_TEST_COMMAND_LOG": str(self.command_log),
            "XKCNSU_TEST_CAPTURE": str(self.capture),
            "CARGO_TARGET_DIR": str(self.root / "target"),
            "LIBCLANG_PATH": str(self.root / "unused-clang"),
        })
        self.environment.pop("BASH_ENV", None)
        self.environment.pop("ENV", None)

    def release(self):
        return subprocess.run(
            ["bash", str(self.root / "tools/build-release.sh"), "ksud"],
            cwd=self.root, env=self.environment, capture_output=True, text=True, timeout=20,
        )

    def assert_not_built(self):
        self.assertFalse(self.command_log.exists())
        self.assertFalse(self.capture.exists())
        self.assertFalse((self.out / "manifests/embedded-assets-aarch64.json").exists())
        self.assertFalse((self.out / "ksud").exists())

    def assert_build_inputs(self, abi):
        arch, triple = self.arches[abi]
        captured = self.capture / arch
        self.assertEqual((captured / "ksuinit").read_bytes(), self.fresh[abi])
        embedded = {item["name"]: item for item in self.read_json(f"compile-inputs/{arch}.json")}
        names = {"ksuinit", "android14-6.1_kernelsu.ko",
                 "android14-6.1_susfs_guard_lkm.ko"}
        if arch == "aarch64":
            names.update(self.tools)
        self.assertEqual(set(embedded), names)
        for name, item in embedded.items():
            data = (captured / name).read_bytes()
            self.assertEqual(item, {"name": name, "size": len(data), "sha256": sha(data)})
        daemon = (self.root / f"target/{triple}/release/ksud").read_bytes()
        self.assertEqual((self.out / f"ksud/{abi}/ksud").read_bytes(), daemon)
        self.assertEqual((self.work / f"manager/app/src/main/jniLibs/{abi}/libksud.so").read_bytes(),
                         daemon)
        commands = self.command_log.read_text().splitlines()
        self.assertEqual(commands[-2:], [
            f"cargo clean --package ksud --target {triple} --release",
            f"cargo build --locked --package ksud --target {triple} --release",
        ])

    def test_refreshes_stale_init_before_manifest_and_compile_for_both_abis(self):
        for abi in self.arches:
            with self.subTest(abi=abi):
                self.environment["XKCNSU_ABIS"] = abi
                result = self.release()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_build_inputs(abi)
                arch = self.arches[abi][0]
                self.assertEqual((self.work / f"userspace/ksud/bin/{arch}/ksuinit").read_bytes(),
                                 self.fresh[abi])

    def test_x86_daemon_build_does_not_require_arm64_kernel_modules(self):
        self.environment["XKCNSU_ABIS"] = "x86_64"
        for suffix in ("kernelsu", "susfs_guard_lkm"):
            (self.out / f"x86_64/android14-6.1_{suffix}.ko").unlink()
        result = self.release()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            {item["name"] for item in self.read_json("compile-inputs/x86_64.json")},
            {"ksuinit"},
        )
        self.assertTrue((self.out / "ksud/x86_64/ksud").is_file())

    def test_missing_init_output_rejects_even_with_old_snapshot_asset(self):
        (self.out / "ksuinit/arm64-v8a/ksuinit").unlink()
        result = self.release()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Build ksuinit before ksud.", result.stderr)
        self.assert_not_built()
        self.assertEqual((self.work / "userspace/ksud/bin/aarch64/ksuinit").read_bytes(),
                         self.old_init)

    def test_empty_init_output_rejects_even_with_old_snapshot_asset(self):
        self.put("out/ksuinit/arm64-v8a/ksuinit", b"")
        result = self.release()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Build ksuinit before ksud.", result.stderr)
        self.assert_not_built()

    def test_output_init_is_copied_when_snapshot_asset_is_absent(self):
        (self.work / "userspace/ksud/bin/aarch64/ksuinit").unlink()
        result = self.release()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_build_inputs("arm64-v8a")

    def test_arm64_patch_tools_are_refreshed_before_manifest_and_compile(self):
        result = self.release()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_build_inputs("arm64-v8a")
        for name, data in self.tools.items():
            with self.subTest(asset=name):
                self.assertEqual((self.capture / "aarch64" / name).read_bytes(), data)

    def test_missing_patch_tool_output_rejects_instead_of_using_snapshot(self):
        for name, data in self.tools.items():
            with self.subTest(asset=name):
                (self.out / "kpatch/aarch64" / name).unlink()
                result = self.release()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Missing source-built " + name, result.stderr)
                self.assert_not_built()
                self.put("out/kpatch/aarch64/" + name, data)

    def test_empty_patch_tool_output_rejects_instead_of_using_snapshot(self):
        for name, data in self.tools.items():
            with self.subTest(asset=name):
                self.put("out/kpatch/aarch64/" + name, b"")
                result = self.release()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Missing source-built " + name, result.stderr)
                self.assert_not_built()
                self.put("out/kpatch/aarch64/" + name, data)

    def test_partial_matrix_keeps_the_same_native_input_checks(self):
        self.environment["XKCNSU_REQUIRE_FULL_MATRIX"] = "0"
        result = self.release()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_build_inputs("arm64-v8a")
        self.assertEqual(self.command_log.read_text().splitlines()[0],
                         f"python3 {self.root}/tools/build-verify-modules.py "
                         "--kmi android14-6.1 --kmi android12-5.10")


if __name__ == "__main__":
    unittest.main()
