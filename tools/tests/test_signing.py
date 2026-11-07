#!/usr/bin/env python3
"""Host signing tests using real, disposable PKCS12 identities.

Run on Linux, including WSL:
  python3 -B -m unittest discover -s tools/tests -p test_signing.py -v

Every signing command uses copied scripts and an isolated temporary root.
No repository output, existing keystore, or real signing password is read.
"""

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import unittest


TOOLS = Path(__file__).resolve().parents[1]
DEV_ALIAS = "xkcnsu-dev"
RELEASE_ALIAS = "xkcnsu-release"
REQUIRED_TOOLS = ("bash", "keytool", "openssl", "python3", "flock")

KEYTOOL_WRAPPER = r"""#!/usr/bin/env bash
set -euo pipefail
operation="${1:-}"
printf '%s\n' "$operation" >>"$XKCNSU_TEST_KEYTOOL_LOG"
"$XKCNSU_TEST_REAL_KEYTOOL" "$@"
if [[ "$operation" == -importkeystore && "${XKCNSU_TEST_FAIL_AFTER_IMPORT:-0}" == 1 ]]; then
    printf 'Injected import completion failure.\n' >&2
    exit 67
fi
if [[ "$operation" == -exportcert && "${XKCNSU_TEST_CORRUPT_PROMOTED_CERT:-0}" == 1 ]]; then
    previous=""
    destination=""
    for argument in "$@"; do
        if [[ "$previous" == -file ]]; then
            destination="$argument"
        fi
        previous="$argument"
    done
    if [[ "$destination" == */promoted.der ]]; then
        cp -- "$XKCNSU_TEST_OTHER_CERT" "$destination"
    fi
fi
"""


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipUnless(
    os.name == "posix" and all(shutil.which(name) for name in REQUIRED_TOOLS),
    "Requires Linux/WSL with Bash, keytool, OpenSSL, Python3 and flock",
)
class SigningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.class_temp = tempfile.TemporaryDirectory(prefix="xkcnsu signing fixtures ")
        cls.addClassCleanup(cls.class_temp.cleanup)
        cls.fixture_root = Path(cls.class_temp.name).resolve()
        cls.real_keytool = shutil.which("keytool")
        cls.real_openssl = shutil.which("openssl")
        cls.signing_source = (TOOLS / "build-signing.sh").read_bytes()
        cls.common_source = (TOOLS / "build-common.sh").read_bytes()
        cls.fixture_environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(cls.fixture_root / "home"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        (cls.fixture_root / "home").mkdir()
        cls.fixture_secrets = {}
        for stem in (DEV_ALIAS, RELEASE_ALIAS):
            password = "Fixture-" + secrets.token_hex(24)
            password_file = cls.fixture_root / (stem + ".password")
            password_file.write_text(password + "\n", encoding="ascii")
            cls.fixture_secrets[stem] = password
            store = cls.fixture_root / (stem + ".p12")
            cls.fixture_command([
                cls.real_keytool, "-genkeypair", "-noprompt",
                "-storetype", "PKCS12", "-keystore", str(store),
                "-alias", stem, "-keyalg", "RSA", "-keysize", "2048",
                "-sigalg", "SHA256withRSA", "-validity", "2",
                "-dname", "CN=Disposable XKCNSU Signing Test",
                "-storepass:file", str(password_file),
                "-keypass:file", str(password_file),
            ])
            cls.fixture_command([
                cls.real_keytool, "-exportcert", "-keystore", str(store),
                "-alias", stem, "-storepass:file", str(password_file),
                "-file", str(cls.fixture_root / (stem + ".der")),
            ])

    @classmethod
    def fixture_command(cls, command):
        result = subprocess.run(
            command, env=cls.fixture_environment, capture_output=True, timeout=30,
        )
        if result.returncode:
            raise RuntimeError("Disposable keytool fixture operation failed")
        return result

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="xkcnsu signing tests ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.out = self.root / "out"
        self.private = self.out / "private"
        self.private.mkdir(parents=True)
        self.put("tools/build-signing.sh", self.signing_source)
        self.put("tools/build-common.sh", self.common_source)
        self.put("kernel/manager/apk_sign.c", b"#define CERT_MAX_LENGTH 4096\n")
        self.put("bin/keytool", KEYTOOL_WRAPPER.encode("ascii")).chmod(0o700)
        self.command_log = self.root / "keytool-operations.log"
        self.environment = {
            "PATH": str(self.root / "bin") + ":/usr/bin:/bin",
            "HOME": str(self.root / "home"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "SOURCE_DATE_EPOCH": "1700000000",
            "XKCNSU_ROOT": str(self.root),
            "XKCNSU_OUT": str(self.out),
            "XKCNSU_CACHE": str(self.root / "cache"),
            "XKCNSU_TMPDIR": str(self.root / "cache/tmp"),
            "XKCNSU_PACKAGE_NAME": "io.xkcnsu.manager",
            "XKCNSU_TEST_KEYTOOL_LOG": str(self.command_log),
            "XKCNSU_TEST_REAL_KEYTOOL": self.real_keytool,
            "XKCNSU_TEST_OTHER_CERT": str(self.fixture_root / (RELEASE_ALIAS + ".der")),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        (self.root / "home").mkdir()

    def put(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def install_development(self):
        for suffix in (".p12", ".der"):
            shutil.copyfile(
                self.fixture_root / (DEV_ALIAS + suffix),
                self.private / (DEV_ALIAS + suffix),
            )
        shutil.copyfile(
            self.fixture_root / (DEV_ALIAS + ".password"),
            self.private / "keystore-password",
        )
        cert = self.private / (DEV_ALIAS + ".der")
        metadata = {
            "package_name": "io.xkcnsu.manager",
            "key_alias": DEV_ALIAS,
            "certificate_der": str(cert),
            "keystore": str(self.private / (DEV_ALIAS + ".p12")),
            "certificate_size": cert.stat().st_size,
            "KSU_PACKAGE_NAME": "io.xkcnsu.manager",
            "KSU_EXPECTED_SIZE": f"0x{cert.stat().st_size:04x}",
            "KSU_EXPECTED_HASH": sha(cert),
            "development_only": True,
        }
        self.put("out/private/certificate.json", json.dumps(metadata).encode("utf-8"))

    def install_distinct_release(self):
        shutil.copyfile(
            self.fixture_root / (RELEASE_ALIAS + ".p12"),
            self.private / (RELEASE_ALIAS + ".p12"),
        )
        shutil.copyfile(
            self.fixture_root / (RELEASE_ALIAS + ".password"),
            self.private / "release-password",
        )

    def material_hashes(self):
        return {
            str(path.relative_to(self.private)): sha(path)
            for path in self.private.rglob("*")
            if path.is_file() and path.name != "signing.lock"
        }

    def operations(self):
        if not self.command_log.exists():
            return []
        return self.command_log.read_text(encoding="utf-8").splitlines()

    def assert_secret_free(self, output):
        passwords = list(self.fixture_secrets.values())
        for name in ("keystore-password", "release-password"):
            path = self.private / name
            if path.is_file():
                value = path.read_text(encoding="ascii").strip()
                if value:
                    passwords.append(value)
        for password in passwords:
            if password in output:
                self.fail("A disposable signing password leaked to captured output")
        for key_type in ("PRIVATE KEY", "RSA PRIVATE KEY"):
            marker = "".join(("-----BEGIN ", key_type, "-----"))
            if marker in output:
                self.fail("Private key material leaked to captured output")

    def sign(self, *arguments, profile=None, extra_environment=None):
        environment = dict(self.environment)
        if profile is not None:
            environment["XKCNSU_SIGNING_PROFILE"] = profile
        if extra_environment:
            environment.update(extra_environment)
        result = subprocess.run(
            ["bash", str(self.root / "tools/build-signing.sh"), *arguments],
            cwd=self.root, env=environment, capture_output=True, text=True, timeout=45,
        )
        self.assert_secret_free(result.stdout + result.stderr)
        for path in (self.out / "build-logs").glob("*.log"):
            self.assert_secret_free(path.read_text(encoding="utf-8", errors="replace"))
        self.assertFalse(list(self.private.glob(".signing.*")), "Staging material was not cleaned")
        return result

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def metadata(self):
        return json.loads((self.private / "certificate.json").read_text(encoding="utf-8"))

    def assert_no_new_release(self):
        for name in (
            RELEASE_ALIAS + ".p12", RELEASE_ALIAS + ".der",
            RELEASE_ALIAS + ".pem", "release-password",
        ):
            self.assertFalse((self.private / name).exists(), "Unexpected published release material")

    def assert_private_key_usable(self, stem, password_name):
        self.fixture_tool([
            self.real_openssl, "pkcs12",
            "-in", str(self.private / (stem + ".p12")),
            "-passin", "file:" + str(self.private / password_name),
            "-nocerts", "-nodes", "-out", str(self.root / "signing-key.pem"),
        ])
        self.fixture_tool([
            self.real_openssl, "x509", "-inform", "DER",
            "-in", str(self.private / (stem + ".der")),
            "-pubkey", "-noout", "-out", str(self.root / "verification-key.pem"),
        ])
        message = self.put("challenge.bin", secrets.token_bytes(64))
        self.fixture_tool([
            self.real_openssl, "dgst", "-sha256",
            "-sign", str(self.root / "signing-key.pem"),
            "-out", str(self.root / "challenge.sig"), str(message),
        ])
        result = self.fixture_tool([
            self.real_openssl, "dgst", "-sha256",
            "-verify", str(self.root / "verification-key.pem"),
            "-signature", str(self.root / "challenge.sig"), str(message),
        ])
        self.assertIn(b"Verified OK", result.stdout)

    def fixture_tool(self, command):
        result = subprocess.run(
            command, env=self.environment, capture_output=True, timeout=30,
        )
        self.assert_secret_free((result.stdout + result.stderr).decode("utf-8", errors="replace"))
        self.assertEqual(result.returncode, 0, "Disposable crypto verification operation failed")
        return result

    def test_default_release_refuses_to_replace_an_existing_development_identity(self):
        self.install_development()
        before = self.material_hashes()
        result = self.sign()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--promote-development", result.stdout + result.stderr)
        self.assertEqual(self.material_hashes(), before)
        self.assertNotIn("-genkeypair", self.operations())
        self.assertNotIn("-importkeystore", self.operations())
        self.assert_no_new_release()

    def test_private_key_output_guard_rejects_both_complete_pem_headers(self):
        for encoded_header in ("-----BEGIN_PRIVATE_KEY-----", "-----BEGIN_RSA_PRIVATE_KEY-----"):
            with self.subTest(header=encoded_header):
                with self.assertRaisesRegex(AssertionError, "Private key material leaked"):
                    self.assert_secret_free("log prefix\n" + encoded_header.replace("_", " ") + "\n")
        self.assert_secret_free("-----BEGIN CERTIFICATE-----\npublic certificate output\n")

    def test_real_promotion_keeps_certificate_alias_and_usable_private_key(self):
        self.install_development()
        old_store_hash = sha(self.private / (DEV_ALIAS + ".p12"))
        original_der = (self.private / (DEV_ALIAS + ".der")).read_bytes()
        self.assert_ok(self.sign("--promote-development"))
        metadata = self.metadata()
        self.assertEqual((self.private / (RELEASE_ALIAS + ".der")).read_bytes(), original_der)
        self.assertEqual(metadata["KSU_EXPECTED_HASH"], hashlib.sha256(original_der).hexdigest())
        self.assertEqual(metadata["key_alias"], RELEASE_ALIAS)
        self.assertEqual(metadata["signing_profile"], "release")
        self.assertIs(metadata["development_only"], False)
        self.assertEqual(metadata["keystore"], str(self.private / (RELEASE_ALIAS + ".p12")))
        self.assertEqual(metadata["password_file"], str(self.private / "release-password"))
        self.assertEqual(sha(self.private / (DEV_ALIAS + ".p12")), old_store_hash)
        self.assertNotEqual(
            (self.private / "release-password").read_bytes(),
            (self.private / "keystore-password").read_bytes(),
            "Promotion did not rotate the keystore password",
        )
        self.assertNotIn("-genkeypair", self.operations())
        self.assertEqual(self.operations().count("-importkeystore"), 1)
        self.assert_private_key_usable(RELEASE_ALIAS, "release-password")

    def test_repeated_promotion_and_normal_release_do_not_regenerate_identity(self):
        self.install_development()
        self.assert_ok(self.sign("--promote-development"))
        release_store = self.private / (RELEASE_ALIAS + ".p12")
        before = (sha(release_store), (self.private / "release-password").read_bytes())
        self.assert_ok(self.sign("--promote-development"))
        self.assert_ok(self.sign())
        after = (sha(release_store), (self.private / "release-password").read_bytes())
        self.assertEqual(hashlib.sha256(before[1]).digest(), hashlib.sha256(after[1]).digest())
        self.assertEqual(before[0], after[0])
        self.assertEqual(self.operations().count("-importkeystore"), 1)
        self.assertNotIn("-genkeypair", self.operations())
        self.assertNotIn("-selfcert", self.operations())

    def test_missing_development_password_retains_existing_material(self):
        self.install_development()
        (self.private / "keystore-password").unlink()
        before = self.material_hashes()
        result = self.sign("--promote-development")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.material_hashes(), before)
        self.assertFalse(self.operations())
        self.assert_no_new_release()

    def test_incorrect_development_password_does_not_publish_release_material(self):
        self.install_development()
        self.put("out/private/keystore-password", b"Incorrect-Fixture-Password\n")
        before = self.material_hashes()
        result = self.sign("--promote-development")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.material_hashes(), before)
        self.assertNotIn("-genkeypair", self.operations())
        self.assert_no_new_release()

    def test_missing_release_password_does_not_overwrite_keystore_or_metadata(self):
        self.install_development()
        self.assert_ok(self.sign("--promote-development"))
        (self.private / "release-password").unlink()
        before = self.material_hashes()
        operations = list(self.operations())
        result = self.sign()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.material_hashes(), before)
        self.assertEqual(self.operations(), operations)

    def test_orphan_release_password_is_not_replaced_or_used_for_new_identity(self):
        self.put("out/private/release-password", b"Orphan-Fixture-Password\n")
        before = self.material_hashes()
        result = self.sign()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.material_hashes(), before)
        self.assertFalse(self.operations())
        self.assertFalse((self.private / (RELEASE_ALIAS + ".p12")).exists())

    def test_import_failure_cleans_staging_without_publishing_partial_identity(self):
        self.install_development()
        before = self.material_hashes()
        result = self.sign(
            "--promote-development",
            extra_environment={"XKCNSU_TEST_FAIL_AFTER_IMPORT": "1"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("-importkeystore", self.operations())
        self.assertEqual(self.material_hashes(), before)
        self.assert_no_new_release()
        self.assert_ok(self.sign("--promote-development"))

    def test_changed_promotion_certificate_is_rejected_before_publication(self):
        self.install_development()
        before = self.material_hashes()
        result = self.sign(
            "--promote-development",
            extra_environment={"XKCNSU_TEST_CORRUPT_PROMOTED_CERT": "1"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("certificate", (result.stdout + result.stderr).lower())
        self.assertEqual(self.material_hashes(), before)
        self.assert_no_new_release()

    def test_explicit_promotion_rejects_a_different_preexisting_release_identity(self):
        self.install_development()
        self.install_distinct_release()
        before = self.material_hashes()
        result = self.sign("--promote-development")
        self.assertNotEqual(
            result.returncode, 0,
            "Promotion reported success while retaining a different release certificate",
        )
        self.assertEqual(self.material_hashes(), before)
        self.assertNotIn("-genkeypair", self.operations())

    def test_default_new_identity_is_release_and_reused_on_the_second_run(self):
        self.assert_ok(self.sign())
        metadata = self.metadata()
        self.assertEqual(metadata["signing_profile"], "release")
        self.assertIs(metadata["development_only"], False)
        self.assertEqual(metadata["key_alias"], RELEASE_ALIAS)
        self.assertEqual(self.operations().count("-genkeypair"), 1)
        self.assertNotIn("-importkeystore", self.operations())
        self.assertFalse((self.private / (DEV_ALIAS + ".p12")).exists())
        store_hash = sha(self.private / (RELEASE_ALIAS + ".p12"))
        self.assert_ok(self.sign())
        self.assertEqual(sha(self.private / (RELEASE_ALIAS + ".p12")), store_hash)
        self.assertEqual(self.operations().count("-genkeypair"), 1)

    def test_explicit_development_profile_reuses_the_development_certificate(self):
        self.install_development()
        certificate_hash = sha(self.private / (DEV_ALIAS + ".der"))
        self.assert_ok(self.sign(profile="development"))
        metadata = self.metadata()
        self.assertEqual(metadata["KSU_EXPECTED_HASH"], certificate_hash)
        self.assertEqual(metadata["signing_profile"], "development")
        self.assertIs(metadata["development_only"], True)
        self.assertEqual(metadata["key_alias"], DEV_ALIAS)
        self.assertNotIn("-genkeypair", self.operations())
        self.assert_no_new_release()

    def test_invalid_profile_preserves_existing_identity(self):
        self.install_development()
        before = self.material_hashes()
        result = self.sign(profile="relase")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.material_hashes(), before)
        self.assertFalse(self.operations())

    def test_release_rejects_certificate_compaction(self):
        self.install_development()
        before = self.material_hashes()
        result = self.sign("--compact-certificate")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.material_hashes(), before)
        self.assertNotIn("-selfcert", self.operations())
        self.assert_no_new_release()

    def test_development_profile_rejects_promotion(self):
        self.install_development()
        before = self.material_hashes()
        result = self.sign("--promote-development", profile="development")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.material_hashes(), before)
        self.assertFalse(self.operations())


if __name__ == "__main__":
    unittest.main()
