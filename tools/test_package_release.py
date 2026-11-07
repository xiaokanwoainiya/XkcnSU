#!/usr/bin/env python3
"""Packaging regression tests; use synthetic artifacts and no device/build tools."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


SPEC = importlib.util.spec_from_file_location(
    "package_release", Path(__file__).with_name("package-release.py"))
PACKAGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGE)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def elf(label):
    data = bytearray(b"\x7fELF" + b"\0" * 20)
    data[18:20] = (183).to_bytes(2, "little")
    return bytes(data) + label.encode()


def boot_image(kernel):
    header = bytearray(4096)
    header[:8] = b"ANDROID!"
    header[8:12] = len(kernel).to_bytes(4, "little")
    header[40:44] = (4).to_bytes(4, "little")
    return bytes(header) + kernel


class PackageReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="xkcnsu-package-regression-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.serial = "fixture-device-serial"
        self.fingerprint = "fixture/device/fingerprint:16/private-user"
        self.password = "fixture-password-do-not-publish"
        self.raw_personal = "fixture-account@example.invalid"
        self.sources = ["src/main.rs", "LICENSE", "temp.gz", "out/private/signing.p12",
                        "out/device-acceptance/raw.log", ".env", "keys/local.key",
                        "src/accidental-key.txt", "src/local-report.txt",
                        "src/accidental-password.txt", "src/accidental-serial.txt"]
        self.put("src/main.rs", b"fn main() {}\n")
        self.put("LICENSE", b"Fixture license\n")
        self.public_sources = {"src/main.rs"} | PACKAGE.REQUIRED_SOURCES
        for name in sorted(PACKAGE.REQUIRED_SOURCES - {"LICENSE"}):
            data = PACKAGE.SOURCE_LINKS.get(name, "fixture " + name).encode()
            self.put(name, data)
            self.sources.append(name)
        self.put("temp.gz", b"temporary-compressed-config")
        self.put("out/private/signing.p12", b"fixture-private-keystore")
        self.put("out/private/keystore-password", self.password.encode())
        self.put(".env", b"password=private")
        self.put("keys/local.key", b"fixture-private-key")
        key_marker = b"-----BEGIN " + b"PRIVATE KEY-----\nfixture\n"
        self.put("src/accidental-key.txt", key_marker)
        self.put("src/accidental-password.txt", self.password.encode())
        self.put("src/accidental-serial.txt", self.serial.encode())
        self.put("src/local-report.txt", self.fingerprint.encode())
        self.put("out/device-acceptance/raw.log", self.raw_personal.encode())
        self.put("out/device-acceptance/ui.xml", self.raw_personal.encode())
        self.put("out/device-acceptance/screen.png", b"fixture-personal-screenshot")
        self.put("out/offline-validation/kernel.original", b"private-original-kernel")
        self.put("out/device-backup/private/current.img", b"private-partition")
        self.put("out/images/XKCNSU-init_boot-device.img", self.raw_personal.encode())
        self.put("out/images/unlisted.img", self.raw_personal.encode())
        self.put("out/images/screen.png", self.raw_personal.encode())
        self.image_data = {
            "images/XKCNSU-init_boot.img": b"fixture stock-derived root image",
            "images/XKCNSU-KPM-boot.img": boot_image(b"fixture patched stock kernel"),
            "images/XKCNSU-KPM-restored-boot.img": boot_image(b"fixture stock kernel"),
        }
        for relative, data in self.image_data.items():
            self.put("out/" + relative, data)
        self.put("out/stock/fixture/init_boot.img", b"fixture stock init_boot")
        self.put("out/stock/fixture/boot.img",
                 self.image_data["images/XKCNSU-KPM-restored-boot.img"])
        self.json("out/images/install-root.json",
                  {"serial": self.serial, "slot": "_b",
                   "entries": [{"source_sha256": sha(b"private-original-kernel")}]})
        for name in PACKAGE.ACCEPTANCE_NAMES:
            report = {
                "completed": True, "serial": self.serial,
                "fingerprint": self.fingerprint,
                "commands": [{"stdout": self.raw_personal, "stderr": self.password,
                              "arguments": ["/data/adb/private/file"]}],
                "checks": [{"description": self.raw_personal, "passed": True},
                           {"name": self.fingerprint, "passed": True}],
                "private": {"path": str(self.root), "password": self.password},
            }
            if name == "kpm-modules":
                report.update(result="passed", passed_checks=2, error=None)
            self.json("out/device-acceptance/" + name + ".json", report)
        self.json("out/manifests/kpatch-x18-audit.json",
                  {"passed": False, "elf": str(self.root / "private"),
                   "limitation": self.raw_personal})
        self.cache_path = "/opt/private-fixture-cache/test-run"
        self.json("out/manifests/host-kpatch-final.json", {
            "schema": 1, "kind": "kpatch-host-final", "ok": True,
            "sources_unchanged": True, "run_cache": self.cache_path,
            "serial": self.serial, "fingerprint": self.fingerprint,
            "log": self.raw_personal,
            "steps": [{"exit_code": 0, "command": self.password, "log": self.cache_path}],
            "sources": [{"path": "src/main.rs", "bytes": len(b"fn main() {}\n"),
                         "sha256": sha(b"fn main() {}\n"),
                         "sha256_after": sha(b"fn main() {}\n"), "unchanged": True}],
            "results": {
                group: {field: (0 if field in {"tests_failed", "tests_ignored",
                                              "tests_filtered"} else 2) for field in fields}
                for group, fields in PACKAGE.HOST_RESULT_FIELDS.items()
            },
        })
        self.json("out/manifests/kpatch-loader-relocations.json",
                  {"object_sha256": sha(b"fixture loader object"), "passed": True,
                   "unadjusted_rodata_pointers": []})
        self.json("out/offline-validation/image-validation.json",
                  {"completed": True, "device_writes": False,
                   "commands": [
                       {"arguments": ["boot-patch", "--boot",
                                      str(self.root / "out/stock/fixture/init_boot.img"),
                                      "--kmi", "android14-6.1", "--allow-shell",
                                      "--out", str(self.root / "out/images"),
                                      "--out-name", "XKCNSU-init_boot.img"],
                        "exit_code": 0, "stdout": self.raw_personal},
                       {"arguments": ["kpm-image", "patch", "--boot",
                                      str(self.root / "out/stock/fixture/boot.img"),
                                      "--out", str(self.root / "out/images/XKCNSU-KPM-boot.img"),
                                      "--kernel-backup", "out/offline-validation/kernel.original",
                                      "--kptools", "out/tools/kptools",
                                      "--kpimg", "out/kpatch/aarch64/kpimg"],
                        "exit_code": 0},
                       {"arguments": ["kpm-image", "restore", "--boot",
                                      "out/images/XKCNSU-KPM-boot.img",
                                      "--kernel", "out/offline-validation/kernel.original",
                                      "--out", "out/images/XKCNSU-KPM-restored-boot.img"],
                        "exit_code": 0},
                   ],
                   "root_image": {"preserved_original_init": True,
                                  "native_assets_match": True,
                                   "sha256": sha(self.image_data["images/XKCNSU-init_boot.img"]),
                                   "bytes": len(self.image_data["images/XKCNSU-init_boot.img"]),
                                   "ksu_config_entry": {
                                       "sha256": sha(b"allow_shell=1 bundled=1")},
                                  "analysis": {"serial": self.serial}},
                   "kpm_roundtrip": {"kernel_identical": True,
                                      "whole_image_identical": True,
                                      "original_kernel_sha256": sha(b"fixture stock kernel"),
                                      "restored_kernel_sha256": sha(b"fixture stock kernel"),
                                      "patched_image_sha256": sha(
                                          self.image_data["images/XKCNSU-KPM-boot.img"])}})
        self.binary_data = {
            "ksud/arm64-v8a/ksud": elf("daemon"),
            "aarch64/android14-6.1_kernelsu.ko": elf("root-module"),
            "aarch64/android14-6.1_susfs_guard_lkm.ko": elf("guard-module"),
            "kpatch/aarch64/kpimg": b"fixture-engine-image",
            "kpatch/aarch64/kptools": elf("patch-tool"),
            "ksuinit/arm64-v8a/ksuinit": elf("init"),
        }
        for relative, data in self.binary_data.items():
            self.put("out/" + relative, data)
        offline = self.read_json("out/offline-validation/image-validation.json")
        offline["root_image"]["analysis"]["ramdisk"] = {
            "entries": [{"path": name, "sha256": sha(self.binary_data[binary])}
                        for name, binary in
                        (("init", "ksuinit/arm64-v8a/ksuinit"),
                         ("kernelsu.ko", "aarch64/android14-6.1_kernelsu.ko"))],
        }
        self.json("out/offline-validation/image-validation.json", offline)
        self.put("out/kpatch/aarch64/kpimg.elf", b"unneeded-engine-debug-symbols")
        self.put("out/ksud/arm64-v8a/diagnostics.log", self.raw_personal.encode())
        self.put("out/aarch64/signing.p12", b"unrelated-private-key")
        self.apk = self.make_apk()
        self.put("out/manager/XKCNSU-dev.apk", self.apk)
        self.json("out/manifests/final-apk.json",
                  {"apk": str(self.root / "out/manager/XKCNSU-dev.apk"),
                   "sha256": sha(self.apk), "size": len(self.apk),
                   "ksud_sha256": sha(self.binary_data["ksud/arm64-v8a/ksud"]),
                   "package_name": "io.xkcnsu.manager", "certificate_sha256": "a" * 64,
                   "serial": self.serial, "password": self.password})
        modules, embedded = [], []
        for relative, data in self.binary_data.items():
            name = Path(relative).name
            entry = {"name": name, "size": len(data), "sha256": sha(data)}
            if name.endswith(".ko"):
                modules.append(entry)
            if name != "ksud":
                embedded.append(entry)
        self.json("out/manifests/verified-modules.json",
                  {"errors": [], "artifacts": modules,
                   "signing": {"keystore": str(self.root / "out/private/signing.p12"),
                               "keystore_pass": self.password}})
        self.json("out/manifests/embedded-assets-aarch64.json", embedded)

    def put(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def json(self, relative, value):
        return self.put(relative, json.dumps(value).encode())

    def read_json(self, relative):
        return json.loads((self.root / relative).read_bytes())

    def make_apk(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("lib/arm64-v8a/libksud.so",
                             self.binary_data["ksud/arm64-v8a/ksud"])
            archive.writestr("AndroidManifest.xml", b"fixture-manifest")
        return output.getvalue()

    def plan(self):
        return PACKAGE.build_plan(self.root, self.sources)

    def assert_public_metadata(self, value):
        if isinstance(value, dict):
            forbidden = {"serial", "device_serial", "fingerprint", "commands",
                         "stdout", "stderr", "password", "keystore", "signing",
                         "source_sha256", "candidate_sha256", "states"}
            self.assertFalse(forbidden.intersection(value))
            for child in value.values():
                self.assert_public_metadata(child)
        elif isinstance(value, list):
            for child in value:
                self.assert_public_metadata(child)
        elif isinstance(value, str):
            for secret in [self.serial, self.password, self.fingerprint,
                           self.raw_personal, str(self.root)]:
                self.assertNotIn(secret, value)

    def test_archives_keep_binaries_and_drop_private_local_evidence(self):
        with mock.patch.object(PACKAGE.subprocess, "check_output") as git, \
                mock.patch.object(PACKAGE.subprocess, "run") as process:
            report = PACKAGE.package_release(self.root, self.sources)
            git.assert_not_called()
            process.assert_not_called()
        output = self.root / "out/delivery"
        self.assertEqual({p.name for p in output.iterdir()}, PACKAGE.DELIVERY_NAMES)
        with zipfile.ZipFile(output / report["source"]["path"]) as archive:
            self.assertEqual(set(archive.namelist()),
                             {"XKCNSU/" + name for name in self.public_sources |
                              PACKAGE.SOURCE_GENERATED})
            self.assertEqual(archive.read("XKCNSU/src/main.rs"), b"fn main() {}\n")
            manifest = json.loads(archive.read("XKCNSU/source-manifest.json"))
            self.assert_public_metadata(manifest)
            for item in manifest["files"]:
                data = archive.read("XKCNSU/" + item["name"])
                self.assertEqual((len(data), sha(data)), (item["bytes"], item["sha256"]))
            self.assert_checksums(archive, "XKCNSU/")
        with zipfile.ZipFile(output / report["artifacts"]["path"]) as archive:
            expected = set(self.binary_data) | {
                "XKCNSU-dev.apk", "manifests/release-artifacts.json",
                "manifests/test-conclusions.json", "manifests/images.json", "SHA256SUMS"}
            self.assertEqual(set(archive.namelist()), expected)
            self.assertEqual(archive.read("XKCNSU-dev.apk"), self.apk)
            for name, data in self.binary_data.items():
                self.assertEqual(archive.read(name), data)
            self.assertFalse(set(self.image_data).intersection(archive.namelist()))
            conclusions = json.loads(archive.read("manifests/test-conclusions.json"))
            self.assertFalse(conclusions["reports"]["kpatch-x18-audit"]["passed"])
            self.assertTrue(conclusions["reports"]["offline-images"]["kpm_roundtrip"]
                            ["whole_image_identical"])
            host = conclusions["reports"]["kpatch-host-final"]
            self.assertTrue(host["sources_verified"])
            self.assertEqual(host["source_files_verified"], 1)
            self.assertEqual(host["results"]["native"]["tests_passed"], 2)
            relocations = conclusions["reports"]["kpatch-loader-relocations"]
            self.assertTrue(relocations["passed"])
            self.assertEqual(relocations["unadjusted_rodata_pointer_count"], 0)
            self.assertNotIn(self.cache_path.encode(), archive.read("manifests/test-conclusions.json"))
            image_list = json.loads(archive.read("manifests/images.json"))
            self.assertEqual(image_list["images"], [])
            self.assertFalse(image_list["images_included"])
            self.assertTrue(image_list["development_acceptance"])
            self.assertFalse(image_list["original_backups_included"])
            self.assertEqual(report["apk"]["certificate_sha256"], "a" * 64)
            self.assert_checksums(archive)
            for name in archive.namelist():
                data = archive.read(name)
                if name.endswith(".json"):
                    self.assert_public_metadata(json.loads(data))
                for secret in [self.serial, self.password, self.fingerprint,
                               self.raw_personal, str(self.root)]:
                    self.assertNotIn(secret.encode(), data, name)
        metadata = (output / "delivery.json").read_bytes()
        self.assert_public_metadata(json.loads(metadata))
        self.assertNotIn(str(self.root).encode(), metadata)
        self.assertNotIn(self.serial.encode(), metadata)
        self.assertFalse(report["history_bundle_included"])
        self.assertFalse(report["exact_device_backups_included"])
        self.assertFalse(report["images_included"])
        self.assertTrue(report["development_acceptance"])
        for kind in ("source", "artifacts"):
            data = (output / report[kind]["path"]).read_bytes()
            self.assertEqual((len(data), sha(data)),
                             (report[kind]["bytes"], report[kind]["sha256"]))

    def assert_checksums(self, archive, prefix=""):
        checksum = prefix + "SHA256SUMS"
        covered = set()
        for line in archive.read(checksum).decode().splitlines():
            expected, name = line.split("  ", 1)
            self.assertEqual(sha(archive.read(prefix + name)), expected)
            covered.add(prefix + name)
        links = {item.filename for item in archive.infolist()
                 if ((item.external_attr >> 16) & 0o170000) == 0o120000}
        self.assertEqual(covered, set(archive.namelist()) - {checksum} - links)

    def test_boolean_failures_are_preserved_and_block_required_acceptance(self):
        report = self.read_json("out/device-acceptance/root.json")
        report["checks"][0]["passed"] = False
        self.assertFalse(PACKAGE.public_results(report)["checks"][0]["passed"])
        self.json("out/device-acceptance/root.json", report)
        with self.assertRaisesRegex(PACKAGE.PackagingError, "failed check"):
            PACKAGE.package_release(self.root, self.sources)
        self.assertFalse((self.root / "out/delivery").exists())

    def test_kpm_failure_cannot_be_turned_into_pass(self):
        report = self.read_json("out/device-acceptance/kpm-modules.json")
        report["result"] = "failed"
        self.json("out/device-acceptance/kpm-modules.json", report)
        with self.assertRaisesRegex(PACKAGE.PackagingError, "has not passed"):
            self.plan()

    def test_missing_final_report_does_not_fall_back_to_interim(self):
        original = self.read_json("out/device-acceptance/final.json")
        self.json("out/device-acceptance/final-interim.json", original)
        (self.root / "out/device-acceptance/final.json").unlink()
        with self.assertRaisesRegex(PACKAGE.PackagingError, "missing.*final.json"):
            self.plan()

    def test_stale_binaries_and_wrong_architecture_are_rejected(self):
        for path in ["out/manager/XKCNSU-dev.apk", "out/ksud/arm64-v8a/ksud",
                     "out/aarch64/android14-6.1_kernelsu.ko",
                     "out/kpatch/aarch64/kpimg", "out/ksuinit/arm64-v8a/ksuinit"]:
            with self.subTest(path=path):
                original = (self.root / path).read_bytes()
                self.put(path, original + b"stale")
                with self.assertRaises(PACKAGE.PackagingError):
                    self.plan()
                self.put(path, original)
        with self.assertRaisesRegex(PACKAGE.PackagingError, "AArch64"):
            PACKAGE.add_binary(self.root, {}, "src/main.rs", "wrong", set(), elf=True)

    def test_module_errors_and_incomplete_pairs_are_rejected(self):
        report = self.read_json("out/manifests/verified-modules.json")
        report["errors"] = ["private build output must not be published"]
        self.json("out/manifests/verified-modules.json", report)
        with self.assertRaisesRegex(PACKAGE.PackagingError, "has not passed"):
            self.plan()
        report["errors"] = []
        report["artifacts"].pop()
        self.json("out/manifests/verified-modules.json", report)
        with self.assertRaisesRegex(PACKAGE.PackagingError, "Incomplete KMI"):
            self.plan()

    def test_path_traversal_and_symlink_inputs_are_rejected(self):
        with self.assertRaises(PACKAGE.PackagingError):
            PACKAGE.build_plan(self.root, ["../private.txt"])
        target = self.root / "src/main.rs"
        link = self.root / "src/link.rs"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("Host does not permit symlink creation")
        with self.assertRaisesRegex(PACKAGE.PackagingError, "uses a link"):
            PACKAGE.build_plan(self.root, ["src/link.rs"])

    def test_legacy_delivery_bundle_blocks_publication(self):
        self.put("out/delivery/XKCNSU-history.bundle", b"private-history")
        with self.assertRaisesRegex(PACKAGE.PackagingError, "legacy"):
            PACKAGE.package_release(self.root, self.sources)
        self.assertEqual((self.root / "out/delivery/XKCNSU-history.bundle").read_bytes(),
                         b"private-history")

    def test_git_selection_is_read_only_and_never_creates_a_history_bundle(self):
        listed = "\0".join(self.sources).encode() + b"\0"
        with mock.patch.object(PACKAGE.subprocess, "check_output", return_value=listed) as git:
            plan = PACKAGE.build_plan(self.root)
        arguments = git.call_args.args[0]
        self.assertIn("ls-files", arguments)
        self.assertIn("--no-optional-locks", arguments)
        self.assertEqual(git.call_args.kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(set(plan["source"]),
                         {"XKCNSU/" + name for name in self.public_sources |
                          PACKAGE.SOURCE_GENERATED})

    def test_cli_root_and_check_only_are_compatible_without_delivery_writes(self):
        with mock.patch.object(PACKAGE, "source_candidates", return_value=self.sources), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            PACKAGE.main(["--root", str(self.root), "--check-only"])
        report = json.loads(output.getvalue())
        self.assertTrue(report["validated"])
        self.assertFalse(report["delivery_written"])
        self.assertFalse((self.root / "out/delivery").exists())

    def test_source_policy_excludes_temporary_and_private_formats(self):
        for name in ["temp.gz", "keys/local.p12", "keys/local.p8", "id_rsa",
                     "credentials.txt", ".env.local", "out/anything", "archive.bundle",
                     "device-acceptance/ui.xml", "private/notes.txt", "local.properties",
                     "manager/app/build/private.txt", "manager/app/.cxx/state.json",
                     "manager/.kotlin/session.txt", "capture.pcap", "vm.hprof",
                     "kernel/kpimg.elf", "kernel/kernelsu.ko", "kernel/temporary.o"]:
            with self.subTest(name=name):
                self.assertFalse(PACKAGE.source_allowed(name))
        self.assertTrue(PACKAGE.source_allowed("manager/app/src/main/AndroidManifest.xml"))
        self.assertTrue(PACKAGE.source_allowed("tools/package-release.py"))
        self.assertTrue(PACKAGE.source_allowed("manager/gradle/wrapper/gradle-wrapper.jar"))

    def test_release_signing_password_is_removed_from_source_candidates(self):
        password = b"fixture-formal-release-password-not-for-publication"
        self.put("out/private/release-password", password)
        self.put("src/release-password-copy.txt", password)
        plan = PACKAGE.build_plan(self.root, self.sources + ["src/release-password-copy.txt"])
        self.assertNotIn("XKCNSU/src/release-password-copy.txt", plan["source"])
        self.assertTrue(all(password not in data for data in plan["source"].values()))
        self.assertTrue(all(password not in data for data in plan["artifacts"].values()))

    def test_release_profile_uses_formal_apk_instead_of_stale_development_file(self):
        manifest = self.read_json("out/manifests/final-apk.json")
        manifest.update(signing_profile="release", development_only=False)
        self.json("out/manifests/final-apk.json", manifest)
        self.put("out/manager/XKCNSU.apk", self.apk)
        self.put("out/manager/XKCNSU-dev.apk", b"stale development APK")
        plan = self.plan()
        self.assertEqual(plan["artifacts"]["XKCNSU.apk"], self.apk)
        self.assertNotIn("XKCNSU-dev.apk", plan["artifacts"])
        self.assertEqual(plan["apk"]["signing_profile"], "release")
        self.assertIs(plan["apk"]["development_only"], False)

    def test_release_profile_rejects_development_metadata(self):
        manifest = self.read_json("out/manifests/final-apk.json")
        manifest.update(signing_profile="release", development_only=True)
        self.json("out/manifests/final-apk.json", manifest)
        with self.assertRaisesRegex(PACKAGE.PackagingError, "release signing"):
            self.plan()

    def test_source_archives_preserve_tracked_execute_modes_and_uapi_links(self):
        self.put("tools/build-all.sh", b"#!/usr/bin/env bash\ntrue\n")
        self.put("src/helper", b"fixture executable")
        candidates = dict.fromkeys([*self.sources, "src/helper"])
        candidates["manager/gradlew"] = 0o100755
        candidates["src/helper"] = 0o100755
        with mock.patch.object(PACKAGE, "source_candidates", return_value=candidates):
            report = PACKAGE.package_release(self.root)
        with zipfile.ZipFile(self.root / "out/delivery" / report["source"]["path"]) as archive:
            for name in ("manager/gradlew", "tools/build-all.sh", "src/helper"):
                info = archive.getinfo("XKCNSU/" + name)
                self.assertEqual(info.create_system, 3)
                self.assertEqual(info.external_attr >> 16, 0o100755)
            for name, target in PACKAGE.SOURCE_LINKS.items():
                info = archive.getinfo("XKCNSU/" + name)
                self.assertEqual(info.create_system, 3)
                self.assertEqual(info.external_attr >> 16, 0o120777)
                self.assertEqual(archive.read(info), target.encode())
            notes = archive.read("XKCNSU/BUILDING-SOURCE.md").decode()
            self.assertIn("git init", notes)
            self.assertIn("XKCNSU_SOURCE_ROOT", notes)
            self.assertIn("not the original signed APK bit for bit", notes)

    def test_source_license_and_provenance_cannot_be_silently_omitted(self):
        for name in ("LICENSE", "vendor/kpatch-next/LICENSE",
                     "kernel/susfs/XKCNSU_PROVENANCE.json", "manager/gradlew"):
            with self.subTest(name=name):
                candidates = [path for path in self.sources if path != name]
                with self.assertRaisesRegex(PACKAGE.PackagingError,
                                            "Required source/license/provenance"):
                    PACKAGE.build_plan(self.root, candidates)
        self.put("LICENSE", self.password.encode())
        with self.assertRaisesRegex(PACKAGE.PackagingError,
                                    "Required source/license/provenance"):
            self.plan()

    def test_packager_source_passes_its_secret_filter_and_is_required(self):
        source = Path(PACKAGE.__file__).read_bytes()
        self.assertTrue(PACKAGE.safe_content(source, set()))
        self.assertIn("tools/package-release.py", PACKAGE.REQUIRED_SOURCES)
        self.put("tools/package-release.py", source)
        plan = self.plan()
        self.assertEqual(plan["source"]["XKCNSU/tools/package-release.py"], source)

    def test_uapi_link_targets_cannot_point_to_private_material(self):
        name = "kernel/include/uapi"
        self.put(name, b"../../out/private")
        with self.assertRaisesRegex(PACKAGE.PackagingError, "source link target"):
            self.plan()

    def test_allowed_uapi_symlink_is_preserved_without_dereferencing_it(self):
        name = "kernel/include/uapi"
        path = self.root / name
        path.unlink()
        try:
            path.symlink_to(PACKAGE.SOURCE_LINKS[name], target_is_directory=True)
        except OSError:
            self.skipTest("Host does not permit symlink creation")
        plan = self.plan()
        self.assertEqual(plan["source"]["XKCNSU/" + name], b"../../uapi")
        self.assertEqual(plan["source_modes"]["XKCNSU/" + name], 0o120777)

    def test_git_index_modes_and_merge_conflicts_are_checked(self):
        listed = (b"100755 " + b"a" * 40 + b" 0\tmanager/gradlew\0"
                  b"120000 " + b"b" * 40 + b" 0\tkernel/include/uapi\0"
                  b"tools/untracked.sh\0")
        with mock.patch.object(PACKAGE.subprocess, "check_output", return_value=listed):
            candidates = PACKAGE.source_candidates(self.root)
        self.assertEqual(candidates, {"manager/gradlew": 0o100755,
                                      "kernel/include/uapi": 0o120000,
                                      "tools/untracked.sh": None})
        conflict = b"100644 " + b"a" * 40 + b" 2\tsrc/main.rs\0"
        with mock.patch.object(PACKAGE.subprocess, "check_output", return_value=conflict):
            with self.assertRaisesRegex(PACKAGE.PackagingError, "unresolved merge"):
                PACKAGE.source_candidates(self.root)

    def test_apk_cannot_claim_a_different_embedded_daemon(self):
        self.binary_data["ksud/arm64-v8a/ksud"] = elf("stale-embedded-daemon")
        apk = self.make_apk()
        self.put("out/manager/XKCNSU-dev.apk", apk)
        report = self.read_json("out/manifests/final-apk.json")
        report.update(sha256=sha(apk), size=len(apk))
        self.json("out/manifests/final-apk.json", report)
        with self.assertRaisesRegex(PACKAGE.PackagingError, "embedded daemon differs"):
            self.plan()

    def test_non_apk_payload_is_rejected_even_with_matching_manifest(self):
        apk = b"arbitrary bytes are not an APK"
        self.put("out/manager/XKCNSU-dev.apk", apk)
        report = self.read_json("out/manifests/final-apk.json")
        report.update(sha256=sha(apk), size=len(apk))
        self.json("out/manifests/final-apk.json", report)
        with self.assertRaisesRegex(PACKAGE.PackagingError, "Invalid APK"):
            self.plan()

    def test_completed_required_acceptance_needs_actual_checks(self):
        report = self.read_json("out/device-acceptance/final.json")
        report["checks"] = []
        self.json("out/device-acceptance/final.json", report)
        with self.assertRaisesRegex(PACKAGE.PackagingError, "has no checks"):
            self.plan()

    def test_checksum_paths_reject_newlines(self):
        with self.assertRaisesRegex(PACKAGE.PackagingError, "Unsafe package input path"):
            PACKAGE.build_plan(self.root, [*self.sources, "src/file\nforged"])

    def test_final_host_report_is_required_without_falling_back_to_older_reports(self):
        (self.root / "out/manifests/host-kpatch-final.json").unlink()
        with self.assertRaisesRegex(PACKAGE.PackagingError, "missing.*host-kpatch-final.json"):
            self.plan()

    def test_host_report_checks_the_source_bytes_selected_for_delivery(self):
        self.put("src/main.rs", b"fn main() { changed(); }\n")
        with self.assertRaisesRegex(PACKAGE.PackagingError, "host source differs"):
            self.plan()

    def test_host_report_drops_cache_paths_logs_and_unknown_result_fields(self):
        report = self.read_json("out/manifests/host-kpatch-final.json")
        report["results"]["native"]["raw_log"] = self.raw_personal
        report["results"]["unexpected-group"] = {"path": self.cache_path}
        report["sources"][0]["log"] = self.cache_path
        self.json("out/manifests/host-kpatch-final.json", report)
        plan = self.plan()
        data = plan["artifacts"]["manifests/test-conclusions.json"]
        summary = json.loads(data)["reports"]["kpatch-host-final"]
        self.assertEqual(set(summary), {"ok", "sources_verified", "source_files_verified",
                                        "results"})
        self.assertEqual(set(summary["results"]), set(PACKAGE.HOST_RESULT_FIELDS))
        for group, fields in PACKAGE.HOST_RESULT_FIELDS.items():
            self.assertEqual(set(summary["results"][group]), set(fields))
        self.assert_public_metadata(summary)
        for private in (self.cache_path, self.raw_personal, self.serial, self.password):
            self.assertNotIn(private.encode(), data)

    def test_host_source_paths_evidence_and_duplicate_entries_are_checked(self):
        original = self.read_json("out/manifests/host-kpatch-final.json")
        for mutation in ("missing", "duplicate", "absolute", "traversal",
                         "changed", "after-digest", "size", "private"):
            with self.subTest(mutation=mutation):
                report = json.loads(json.dumps(original))
                item = report["sources"][0]
                if mutation == "missing":
                    report["sources"] = []
                elif mutation == "duplicate":
                    report["sources"].append(dict(item))
                elif mutation == "absolute":
                    item["path"] = self.cache_path
                elif mutation == "traversal":
                    item["path"] = "../src/main.rs"
                elif mutation == "changed":
                    item["unchanged"] = False
                elif mutation == "after-digest":
                    item["sha256_after"] = "b" * 64
                elif mutation == "size":
                    item["bytes"] = True
                else:
                    item["path"] = "out/private/signing.p12"
                self.json("out/manifests/host-kpatch-final.json", report)
                with self.assertRaises(PACKAGE.PackagingError):
                    self.plan()

    def test_host_failures_and_invalid_counts_cannot_be_published_as_passes(self):
        original = self.read_json("out/manifests/host-kpatch-final.json")
        for mutation in ("failed", "source-changed", "step-failed", "steps-missing",
                         "tests-failed", "invalid-count", "missing-group"):
            with self.subTest(mutation=mutation):
                report = json.loads(json.dumps(original))
                if mutation == "failed":
                    report["ok"] = False
                elif mutation == "source-changed":
                    report["sources_unchanged"] = False
                elif mutation == "step-failed":
                    report["steps"][0]["exit_code"] = 1
                elif mutation == "steps-missing":
                    report["steps"] = []
                elif mutation == "tests-failed":
                    report["results"]["native"]["tests_failed"] = 1
                elif mutation == "invalid-count":
                    report["results"]["loader"]["groups_passed"] = True
                else:
                    del report["results"]["supercall"]
                self.json("out/manifests/host-kpatch-final.json", report)
                with self.assertRaises(PACKAGE.PackagingError):
                    self.plan()

    def test_loader_relocation_summary_excludes_paths_identifiers_and_raw_details(self):
        report = self.read_json("out/manifests/kpatch-loader-relocations.json")
        report.update(object=self.cache_path, serial=self.serial, fingerprint=self.fingerprint,
                      log=self.raw_personal, commands=[self.password],
                      unadjusted_rodata_pointer_count=999)
        self.json("out/manifests/kpatch-loader-relocations.json", report)
        data = self.plan()["artifacts"]["manifests/test-conclusions.json"]
        summary = json.loads(data)["reports"]["kpatch-loader-relocations"]
        self.assertEqual(summary, {"passed": True,
                                   "object_sha256": sha(b"fixture loader object"),
                                   "unadjusted_rodata_pointer_count": 0})
        self.assert_public_metadata(summary)
        for private in (self.cache_path, self.raw_personal, self.serial, self.password):
            self.assertNotIn(private.encode(), data)

    def test_present_loader_relocation_failures_and_malformed_reports_block_delivery(self):
        original = self.read_json("out/manifests/kpatch-loader-relocations.json")
        for mutation in ("failed", "invalid-boolean", "invalid-hash", "missing-hash",
                         "invalid-evidence", "unsafe-pointers", "error"):
            with self.subTest(mutation=mutation):
                report = dict(original)
                if mutation == "failed":
                    report["passed"] = False
                elif mutation == "invalid-boolean":
                    report["passed"] = "true"
                elif mutation == "invalid-hash":
                    report["object_sha256"] = self.cache_path
                elif mutation == "missing-hash":
                    del report["object_sha256"]
                elif mutation == "invalid-evidence":
                    report["unadjusted_rodata_pointers"] = {}
                elif mutation == "unsafe-pointers":
                    report["unadjusted_rodata_pointers"] = [
                        {"section": ".rela.rodata", "relocation": self.raw_personal}]
                else:
                    report["error"] = self.password
                self.json("out/manifests/kpatch-loader-relocations.json", report)
                with self.assertRaises(PACKAGE.PackagingError):
                    self.plan()
                self.assertFalse((self.root / "out/delivery").exists())

    def test_absent_optional_loader_relocation_report_does_not_invent_a_result(self):
        (self.root / "out/manifests/kpatch-loader-relocations.json").unlink()
        conclusions = json.loads(self.plan()["artifacts"]["manifests/test-conclusions.json"])
        self.assertNotIn("kpatch-loader-relocations", conclusions["reports"])

    def test_images_are_never_read_or_selected_even_with_matching_reports(self):
        read_input = PACKAGE.read_input

        def reject_image_read(root, relative, *arguments, **options):
            name = str(relative).replace("\\", "/").lower()
            self.assertFalse(name.startswith(("out/images/", "out/stock/", "out/device-backup/")))
            self.assertNotIn(Path(name).suffix, {".img", ".kernel", ".dump"})
            return read_input(root, relative, *arguments, **options)

        with mock.patch.object(PACKAGE, "read_input", side_effect=reject_image_read):
            plan = self.plan()
        self.assertEqual(plan["images"]["images"], [])
        self.assertFalse(plan["images"]["images_included"])
        self.assertFalse(set(self.image_data).intersection(plan["artifacts"]))
        self.assertNotIn("DEVELOPMENT-IMAGES.md", plan["artifacts"])

    def test_missing_images_do_not_block_verified_generic_artifacts(self):
        for relative in [*("out/" + name for name in self.image_data),
                         "out/stock/fixture/init_boot.img", "out/stock/fixture/boot.img",
                         "out/offline-validation/kernel.original"]:
            (self.root / relative).unlink()
        plan = self.plan()
        self.assertEqual(plan["artifacts"]["XKCNSU-dev.apk"], self.apk)
        for name, data in self.binary_data.items():
            self.assertEqual(plan["artifacts"][name], data)
        self.assertFalse(plan["images"]["images_included"])

    def test_missing_offline_report_still_blocks_publication(self):
        (self.root / "out/offline-validation/image-validation.json").unlink()
        with self.assertRaisesRegex(PACKAGE.PackagingError, "missing"):
            self.plan()

    def test_failed_offline_validation_cannot_be_published_as_passed(self):
        original = self.read_json("out/offline-validation/image-validation.json")
        for mutation in ("incomplete", "device-write", "restore-mismatch", "failed-command",
                         "kernel-mismatch", "error", "native-mismatch", "original-init"):
            with self.subTest(mutation=mutation):
                report = json.loads(json.dumps(original))
                if mutation == "incomplete":
                    report["completed"] = False
                elif mutation == "device-write":
                    report["device_writes"] = True
                elif mutation == "restore-mismatch":
                    report["kpm_roundtrip"]["whole_image_identical"] = False
                elif mutation == "failed-command":
                    report["commands"][1]["exit_code"] = 1
                elif mutation == "kernel-mismatch":
                    report["kpm_roundtrip"]["restored_kernel_sha256"] = "b" * 64
                elif mutation == "error":
                    report["error"] = self.raw_personal
                elif mutation == "native-mismatch":
                    report["root_image"]["native_assets_match"] = False
                else:
                    report["root_image"]["preserved_original_init"] = False
                self.json("out/offline-validation/image-validation.json", report)
                with self.assertRaises(PACKAGE.PackagingError):
                    self.plan()

    def test_matching_native_hashes_do_not_allow_private_content(self):
        original = self.read_json("out/manifests/embedded-assets-aarch64.json")
        for secret in (self.serial.encode(), self.password.encode(),
                       b"-----BEGIN " + b"PRIVATE KEY-----\nfixture\n"):
            with self.subTest(secret=secret):
                private = self.binary_data["kpatch/aarch64/kpimg"] + secret
                self.put("out/kpatch/aarch64/kpimg", private)
                assets = json.loads(json.dumps(original))
                entry = next(item for item in assets if item["name"] == "kpimg")
                entry.update(sha256=sha(private), size=len(private))
                self.json("out/manifests/embedded-assets-aarch64.json", assets)
                with self.assertRaisesRegex(PACKAGE.PackagingError, "Unsafe binary"):
                    self.plan()

    def test_changed_or_private_images_remain_excluded_from_both_archives(self):
        changed = boot_image(b"different stock kernel")
        self.put("out/stock/fixture/boot.img", changed)
        self.put("out/images/XKCNSU-KPM-restored-boot.img", changed)
        self.put("out/images/XKCNSU-init_boot.img", self.raw_personal.encode())
        self.sources += ["images/current.img", "kernel/boot.IMG",
                         "firmware/partition.kernel", "out/stock/fixture/boot.img"]
        for name in self.sources[-4:-1]:
            self.put(name, self.raw_personal.encode())
        report = PACKAGE.package_release(self.root, self.sources)
        for kind in ("source", "artifacts"):
            with zipfile.ZipFile(self.root / "out/delivery" / report[kind]["path"]) as archive:
                for name in archive.namelist():
                    self.assertNotIn(Path(name).suffix.lower(), {".img", ".kernel", ".dump"})
                    self.assertNotIn(self.raw_personal.encode(), archive.read(name))
        self.assertFalse(report["images_included"])

    def test_root_image_native_asset_evidence_must_match_delivered_binaries(self):
        report = self.read_json("out/offline-validation/image-validation.json")
        report["root_image"]["analysis"]["ramdisk"]["entries"][0]["sha256"] = "b" * 64
        self.json("out/offline-validation/image-validation.json", report)
        with self.assertRaisesRegex(PACKAGE.PackagingError, "stale native assets"):
            self.plan()

    def test_ignored_image_links_cannot_select_a_device_image(self):
        target = self.root / "out/images/XKCNSU-init_boot-device.img"
        name = "out/images/XKCNSU-init_boot.img"
        path = self.root / name
        self.put("out/images/XKCNSU-init_boot-device.img", self.image_data["images/XKCNSU-init_boot.img"])
        path.unlink()
        try:
            path.symlink_to(target)
        except OSError:
            self.skipTest("Host does not permit symlink creation")
        plan = self.plan()
        self.assertNotIn("images/XKCNSU-init_boot.img", plan["artifacts"])
        self.assertFalse(plan["images"]["images_included"])

    def test_offline_paths_and_boot_configuration_are_not_public_artifacts(self):
        report = self.read_json("out/offline-validation/image-validation.json")
        private_paths = ["D:\\private-device\\init_boot.img",
                         "/mnt/d/private-device/boot.img", "/data/adb/private/current.img"]
        for command, path in zip(report["commands"], private_paths, strict=True):
            command["arguments"][command["arguments"].index("--boot") + 1] = path
        report["root_image"]["ksu_config_entry"]["sha256"] = sha(b"bundled=1")
        report["commands"][0]["arguments"].remove("--allow-shell")
        self.json("out/offline-validation/image-validation.json", report)
        plan = self.plan()
        summary = plan["artifacts"]["manifests/test-conclusions.json"]
        self.assert_public_metadata(json.loads(summary))
        for path in private_paths:
            self.assertNotIn(path.encode(), summary)
            self.assertNotIn(json.dumps(path).encode(), summary)
        self.assertFalse(plan["images"]["images_included"])

    @unittest.skipUnless(sys.platform != "win32" and shutil.which("unzip")
                         and shutil.which("sha256sum"), "Requires Linux unzip and sha256sum")
    def test_linux_extraction_keeps_links_and_passes_standard_checksums(self):
        candidates = dict.fromkeys(self.sources)
        candidates["manager/gradlew"] = 0o100755
        report = PACKAGE.package_release(self.root, candidates)
        for kind in ("source", "artifacts"):
            destination = self.root / ("extract-" + kind)
            subprocess.run(["unzip", "-q", str(self.root / "out/delivery" / report[kind]["path"]),
                            "-d", str(destination)], check=True, capture_output=True)
            directory = destination / "XKCNSU" if kind == "source" else destination
            result = subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=directory,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            if kind == "source":
                for name, target in PACKAGE.SOURCE_LINKS.items():
                    path = directory / name
                    self.assertTrue(path.is_symlink())
                    self.assertEqual(str(path.readlink()), target)
                    self.assertTrue((path / "ksu.h").is_file())
                self.assertTrue((directory / "manager/gradlew").stat().st_mode & 0o111)


if __name__ == "__main__":
    unittest.main()
