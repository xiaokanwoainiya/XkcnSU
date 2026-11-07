#!/usr/bin/env python3
"""Signing configuration tests using synthetic files and no signing commands."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


TOOLS = Path(__file__).resolve().parents[1]
with mock.patch.object(sys, "path", [str(TOOLS), *sys.path]):
    import signing_config as SIGNING
    SPEC = importlib.util.spec_from_file_location("build_repack", TOOLS / "build-repack.py")
    REPACK = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(REPACK)


class SigningFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="xkcnsu signing config $ ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.private = self.root / "private"
        self.private.mkdir()
        self.certificate = b"synthetic public certificate, not a real signing identity"
        self.password = b" $literal-${project.name}'\\secret space "
        self.paths = {
            "keystore": self.put("private/fixture-store.p12", b"synthetic-keystore"),
            "certificate_der": self.put("private/fixture-certificate.der", self.certificate),
            "password_file": self.put("private/fixture-password", self.password + b"\n"),
        }
        self.metadata_file = self.private / "certificate.json"
        self.metadata = {
            **{key: str(path) for key, path in self.paths.items()},
            "key_alias": "xkcnsu-release",
            "signing_profile": "release",
            "development_only": False,
            "certificate_size": len(self.certificate),
            "KSU_EXPECTED_HASH": hashlib.sha256(self.certificate).hexdigest(),
        }
        self.write_metadata()
        environment = mock.patch.dict(os.environ, {"XKCNSU_REQUIRE_RELEASE_SIGNING": "0"})
        environment.start()
        self.addCleanup(environment.stop)

    def put(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def write_metadata(self, value=None):
        self.metadata_file.write_text(
            json.dumps(self.metadata if value is None else value), encoding="utf-8",
        )

    def load(self, **kwargs):
        return SIGNING.load_signing_config(self.metadata_file, **kwargs)

    def development_metadata(self):
        self.metadata.update(
            key_alias="xkcnsu-dev", signing_profile="development", development_only=True,
        )
        self.write_metadata()


class SigningConfigTests(SigningFixture):
    def test_release_uses_metadata_paths_without_opening_password_or_keystore(self):
        with mock.patch.object(SIGNING, "_read_file", wraps=SIGNING._read_file) as read:
            config = self.load(require_release=True)
        self.assertEqual([call.args[0] for call in read.call_args_list],
                         [self.metadata_file, self.paths["certificate_der"]])
        self.assertEqual(config.keystore, self.paths["keystore"])
        self.assertEqual(config.password_file, self.paths["password_file"])
        self.assertEqual(config.key_alias, "xkcnsu-release")
        self.assertEqual(config.output_name, "XKCNSU")
        self.assertEqual(config.apk_name, "XKCNSU.apk")

    def test_development_still_selects_the_development_apk_name(self):
        self.development_metadata()
        self.assertEqual(self.load().apk_name, "XKCNSU-dev.apk")
        del self.metadata["signing_profile"]
        self.write_metadata()
        self.assertEqual(self.load().signing_profile, "development")

    def test_release_environment_cannot_be_bypassed_by_default_or_false_argument(self):
        self.development_metadata()
        with mock.patch.dict(os.environ, {"XKCNSU_REQUIRE_RELEASE_SIGNING": "1"}):
            for require_release in (None, False, True):
                with self.subTest(require_release=require_release):
                    with self.assertRaisesRegex(SIGNING.SigningConfigError, "release signing"):
                        self.load(require_release=require_release)

    def test_release_requires_explicit_consistent_profile_and_boolean(self):
        original = dict(self.metadata)
        for profile, development in (
            (None, False), ("release", True), ("development", False),
            ("other", False), ("release", 0), ("release", "false"), ("release", None),
        ):
            with self.subTest(profile=profile, development=development):
                self.metadata = dict(original, signing_profile=profile, development_only=development)
                self.write_metadata()
                with self.assertRaises(SIGNING.SigningConfigError):
                    self.load(require_release=True)

    def test_duplicate_or_nonobject_metadata_cannot_override_a_profile(self):
        duplicate = json.dumps(self.metadata)[:-1] + ', "development_only": true}'
        for payload in (duplicate, "[]", "null", '{"invalid":'):
            with self.subTest(payload=payload[:16]):
                self.metadata_file.write_text(payload, encoding="utf-8")
                with self.assertRaises(SIGNING.SigningConfigError):
                    self.load()

    def test_relative_material_paths_resolve_from_metadata_directory(self):
        for key, path in self.paths.items():
            self.metadata[key] = path.name
        self.write_metadata()
        config = self.load()
        self.assertEqual(config.keystore, self.paths["keystore"])
        self.assertEqual(config.password_file, self.paths["password_file"])

    def test_paths_cannot_escape_private_directory_or_traverse_parents(self):
        outside = self.put("outside.p12", b"outside-fixture")
        original = dict(self.metadata)
        for key in self.paths:
            for value in (str(outside), "../outside.p12", str(self.private / "../outside.p12")):
                with self.subTest(key=key, value=value):
                    self.write_metadata(dict(original, **{key: value}))
                    with self.assertRaises(SIGNING.SigningConfigError):
                        self.load()

    def test_missing_empty_or_directory_material_fails_before_password_read(self):
        original = dict(self.metadata)
        empty = self.put("private/empty", b"")
        directory = self.private / "directory"
        directory.mkdir()
        for key in self.paths:
            for replacement in (empty, directory, self.private / "missing"):
                with self.subTest(key=key, replacement=replacement.name):
                    self.write_metadata(dict(original, **{key: str(replacement)}))
                    with self.assertRaises(SIGNING.SigningConfigError):
                        self.load()

    def test_symlinked_material_and_metadata_are_rejected(self):
        link = self.private / "link"
        try:
            link.symlink_to(self.paths["keystore"])
        except OSError:
            self.skipTest("Host does not permit symbolic links")
        original = dict(self.metadata)
        for key in self.paths:
            with self.subTest(key=key):
                self.write_metadata(dict(original, **{key: str(link)}))
                with self.assertRaisesRegex(SIGNING.SigningConfigError, "symbolic links"):
                    self.load()
        self.write_metadata(original)
        link.unlink()
        link.symlink_to(self.metadata_file)
        with self.assertRaisesRegex(SIGNING.SigningConfigError, "symbolic links"):
            SIGNING.load_signing_config(link)

    def test_certificate_hash_and_size_must_match_public_certificate_bytes(self):
        original = dict(self.metadata)
        for mutation in (
            {"KSU_EXPECTED_HASH": "ab" * 32},
            {"KSU_EXPECTED_HASH": "invalid"},
            {"certificate_size": len(self.certificate) + 1},
            {"certificate_size": True},
        ):
            with self.subTest(mutation=mutation):
                self.write_metadata(dict(original, **mutation))
                with self.assertRaises(SIGNING.SigningConfigError):
                    self.load()

    def test_password_preserves_spaces_and_removes_only_one_line_ending(self):
        config = self.load()
        for ending in (b"", b"\n", b"\r\n", b"\r"):
            with self.subTest(ending=ending):
                self.paths["password_file"].write_bytes(self.password + ending)
                self.assertEqual(SIGNING.read_signing_password(config), self.password)

    def test_invalid_password_is_rejected_without_echoing_its_contents(self):
        config = self.load()
        for password in (
            b"", b"\n", b"fixture-secret\nsecond-line\n", b"fixture-secret\n\n",
            b"fixture-secret\0", b"fixture-secret\t", b"fixture-secret\xff",
        ):
            with self.subTest(kind=len(password)):
                self.paths["password_file"].write_bytes(password)
                with self.assertRaises(SIGNING.SigningConfigError) as caught:
                    SIGNING.read_signing_password(config)
                self.assertNotIn("fixture-secret", str(caught.exception))

    def test_gradle_literals_preserve_dollars_quotes_and_backslashes_without_gstrings(self):
        config = self.load()
        output = self.private / "gradle-signing.init.gradle"
        SIGNING.write_gradle_init(config, output)
        source = output.read_text(encoding="utf-8")
        escaped = " $literal-${project.name}\\'\\\\secret space "
        self.assertIn("p.ext.set('KEYSTORE_PASSWORD', '" + escaped + "')", source)
        self.assertIn("p.ext.set('KEY_PASSWORD', '" + escaped + "')", source)
        self.assertIn("p.ext.set('KEY_ALIAS', 'xkcnsu-release')", source)
        self.assertNotIn('"', source)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(list(self.private.glob(".signing-config-*")), [])

    def test_generated_config_cannot_overwrite_material_or_escape_private_directory(self):
        config = self.load()
        before = {path: path.read_bytes() for path in [self.metadata_file, *self.paths.values()]}
        for output in [*before, self.root / "outside.json", self.private / "../outside.json"]:
            with self.subTest(output=output.name):
                with self.assertRaises(SIGNING.SigningConfigError):
                    SIGNING.write_repack_config(config, output, ["arm64-v8a"])
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertFalse((self.root / "outside.json").exists())

    def test_generated_config_rejects_symlink_instead_of_overwriting_its_target(self):
        config = self.load()
        target = self.put("private/unrelated.txt", b"keep-fixture")
        output = self.private / "config.json"
        try:
            output.symlink_to(target)
        except OSError:
            self.skipTest("Host does not permit symbolic links")
        with self.assertRaises(SIGNING.SigningConfigError):
            SIGNING.write_repack_config(config, output, ["arm64-v8a"])
        self.assertEqual(target.read_bytes(), b"keep-fixture")
        self.assertTrue(output.is_symlink())

    def test_atomic_config_replacement_preserves_existing_file_on_publish_failure(self):
        config = self.load()
        output = self.put("private/config.json", b"previous-config")
        with mock.patch.object(SIGNING.os, "replace", side_effect=OSError("fixture publish error")):
            with self.assertRaisesRegex(SIGNING.SigningConfigError, "Cannot write"):
                SIGNING.write_repack_config(config, output, ["arm64-v8a"])
        self.assertEqual(output.read_bytes(), b"previous-config")
        self.assertEqual(list(self.private.glob(".signing-config-*")), [])

    def test_repack_config_has_only_password_tokens_and_metadata_identity(self):
        config = self.load()
        output = self.private / "repack-config.json"
        with mock.patch.object(SIGNING, "read_signing_password", side_effect=AssertionError):
            SIGNING.write_repack_config(config, output, ["arm64-v8a", "x86_64"])
        data = json.loads(output.read_bytes())
        self.assertEqual(data["output_name"], "XKCNSU")
        self.assertEqual(data["arch"], ["arm64-v8a", "x86_64"])
        self.assertEqual(data["signing"], {
            "keystore_path": str(self.paths["keystore"]),
            "key_alias": "xkcnsu-release",
            "keystore_pass": "__XKCNSU_PASSWORD_FILE__",
            "key_pass": "__XKCNSU_PASSWORD_FILE__",
        })
        self.assertNotIn(self.password, output.read_bytes())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_repack_rejects_empty_duplicate_or_unknown_abis(self):
        config = self.load()
        for abis in ([], ["arm64-v8a", "arm64-v8a"], ["unknown"]):
            with self.subTest(abis=abis):
                with self.assertRaises(SIGNING.SigningConfigError):
                    SIGNING.repack_config(config, abis)

    def test_cli_apk_name_never_reads_password_or_prints_signing_paths(self):
        for profile in ("release", "development"):
            with self.subTest(profile=profile):
                if profile == "development":
                    self.development_metadata()
                stdout = io.StringIO()
                with mock.patch.object(sys, "argv", [
                    "signing_config.py", "--metadata", str(self.metadata_file), "apk-name",
                ]), mock.patch.object(SIGNING, "read_signing_password", side_effect=AssertionError), \
                        contextlib.redirect_stdout(stdout):
                    self.assertEqual(SIGNING.main(), 0)
                self.assertEqual(stdout.getvalue(),
                                 "XKCNSU.apk\n" if profile == "release" else "XKCNSU-dev.apk\n")


class BuildRepackSigningTests(SigningFixture):
    def run_repacker(self, *, returncode=0, signer_stdout=""):
        source = self.root / "frozen source"
        expected_name = "XKCNSU" if self.metadata["signing_profile"] == "release" else "XKCNSU-dev"
        stub = SimpleNamespace(merge_config=lambda file_config, arguments: {
            "signing": {"keystore_path": "/wrong-key", "key_alias": "wrong-alias",
                        "keystore_pass": "wrong-password", "key_pass": "wrong-password"},
            "output_name": "wrong-output",
        })
        captured_password_files = []

        def repacker_main():
            config = stub.merge_config({}, SimpleNamespace())
            self.assertEqual(config["output_name"], expected_name)
            self.assertEqual(config["signing"]["keystore_path"], str(self.paths["keystore"]))
            self.assertEqual(config["signing"]["key_alias"], self.metadata["key_alias"])
            stub.run_cmd([
                "apksigner", "sign", "--ks", config["signing"]["keystore_path"],
                "--ks-key-alias", config["signing"]["key_alias"],
                "--ks-pass", "pass:" + config["signing"]["keystore_pass"],
                "--key-pass", "pass:" + config["signing"]["key_pass"],
            ], "fixture signing failed")
            return 0

        def external_command(arguments, **kwargs):
            self.assertEqual(arguments[:6], [
                "apksigner", "sign", "--ks", str(self.paths["keystore"]),
                "--ks-key-alias", self.metadata["key_alias"],
            ])
            self.assertNotIn(self.password.decode(), " ".join(arguments))
            providers = [argument for argument in arguments if argument.startswith("file:")]
            self.assertEqual(len(providers), 2)
            paths = [Path(provider[5:]) for provider in providers]
            self.assertNotEqual(paths[0], paths[1])
            for path in paths:
                self.assertEqual(path.read_bytes(), self.password + b"\n")
                if os.name == "posix":
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            captured_password_files.extend(paths)
            return subprocess.CompletedProcess(arguments, returncode, signer_stdout)

        stub.main = repacker_main
        fake_spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module: None))
        with mock.patch.dict(os.environ, {"XKCNSU_OUT": str(self.root)}), \
                mock.patch.object(sys, "argv", ["build-repack.py", str(source), "repack"]), \
                mock.patch.object(REPACK.importlib.util, "spec_from_file_location", return_value=fake_spec), \
                mock.patch.object(REPACK.importlib.util, "module_from_spec", return_value=stub), \
                mock.patch.object(REPACK.subprocess, "run", side_effect=external_command) as command, \
                mock.patch.object(REPACK, "read_signing_password", wraps=SIGNING.read_signing_password) as read:
            try:
                result = REPACK.main()
            finally:
                self.assertEqual(list(self.private.glob("apksigner-*")), [])
                for path in captured_password_files:
                    self.assertFalse(path.exists())
            command.assert_called_once()
            read.assert_called_once()
        return result

    def test_release_repacker_pins_identity_and_uses_distinct_private_password_files(self):
        with mock.patch.dict(os.environ, {"XKCNSU_REQUIRE_RELEASE_SIGNING": "1"}):
            self.assertEqual(self.run_repacker(), 0)

    def test_development_repacker_keeps_dev_output_but_uses_metadata_password(self):
        self.development_metadata()
        self.assertEqual(self.run_repacker(), 0)

    def test_signer_failure_cleans_provider_files_and_redacts_password_from_diagnostics(self):
        with self.assertRaisesRegex(RuntimeError, "fixture signing failed") as caught:
            self.run_repacker(returncode=1, signer_stdout="tool echoed " + self.password.decode())
        self.assertNotIn(self.password.decode(), str(caught.exception))
        self.assertIn("<redacted>", str(caught.exception))

    def test_formal_gate_rejects_development_before_loading_repacker_or_running_tools(self):
        self.development_metadata()
        with mock.patch.dict(os.environ, {
            "XKCNSU_OUT": str(self.root), "XKCNSU_REQUIRE_RELEASE_SIGNING": "1",
        }), mock.patch.object(sys, "argv", [
            "build-repack.py", str(self.root / "source"), "repack",
        ]), mock.patch.object(REPACK.importlib.util, "spec_from_file_location") as load, \
                mock.patch.object(REPACK.subprocess, "run") as command:
            with self.assertRaisesRegex(SystemExit, "release signing metadata"):
                REPACK.main()
            load.assert_not_called()
            command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
