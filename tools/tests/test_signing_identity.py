"""Use real disposable keystores to reject Manager/kernel identity drift."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


TOOLS = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('keytool'), 'requires the JDK keytool')
class SigningIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory(prefix='xkcnsu-signing-identity-')
        cls.addClassCleanup(cls.fixture.cleanup)
        cls.directory = Path(cls.fixture.name)
        cls.password = 'Disposable-test-password-identity'
        cls.password_file = cls.directory / 'password'
        cls.password_file.write_text(cls.password + '\n')
        for name in ('primary', 'different'):
            result = subprocess.run([
                'keytool', '-genkeypair', '-noprompt', '-storetype', 'PKCS12',
                '-keystore', str(cls.directory / (name + '.p12')),
                '-alias', 'xkcnsu-release', '-keyalg', 'RSA', '-keysize', '2048',
                '-validity', '2', '-dname', 'CN=Disposable XKCNSU identity test',
                '-storepass:file', str(cls.password_file), '-keypass:file', str(cls.password_file),
            ], capture_output=True, timeout=30)
            if result.returncode:
                raise RuntimeError('Cannot create a disposable signing fixture')
            result = subprocess.run([
                'keytool', '-exportcert', '-keystore', str(cls.directory / (name + '.p12')),
                '-alias', 'xkcnsu-release', '-storepass:file', str(cls.password_file),
                '-file', str(cls.directory / (name + '.der')),
            ], capture_output=True, timeout=30)
            if result.returncode:
                raise RuntimeError('Cannot export a disposable public certificate')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='xkcnsu identity check ')
        self.addCleanup(self.temporary.cleanup)
        self.private = Path(self.temporary.name)
        for source, destination in (
            ('primary.p12', 'store.p12'), ('primary.der', 'certificate.der'), ('password', 'password'),
        ):
            shutil.copyfile(self.directory / source, self.private / destination)
        certificate = (self.private / 'certificate.der').read_bytes()
        self.metadata = {
            'package_name': 'io.xkcnsu.manager', 'KSU_PACKAGE_NAME': 'io.xkcnsu.manager',
            'key_alias': 'xkcnsu-release', 'signing_profile': 'release', 'development_only': False,
            'keystore': str(self.private / 'store.p12'),
            'certificate_der': str(self.private / 'certificate.der'),
            'password_file': str(self.private / 'password'),
            'certificate_size': len(certificate), 'KSU_EXPECTED_SIZE': f'0x{len(certificate):04x}',
            'KSU_EXPECTED_HASH': hashlib.sha256(certificate).hexdigest(),
        }

    def verify(self, *extra, package='io.xkcnsu.manager'):
        path = self.private / 'certificate.json'
        path.write_text(json.dumps(self.metadata))
        result = subprocess.run([
            sys.executable, str(TOOLS / 'verify-signing-identity.py'),
            '--metadata', str(path), '--package', package, '--require-release', *extra,
        ], capture_output=True, text=True, timeout=45,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
        self.assertNotIn(self.password, result.stdout + result.stderr)
        return result

    def test_actual_keystore_and_kernel_parameters_share_the_same_certificate(self):
        result = self.verify()
        self.assertEqual(result.returncode, 0, result.stderr)
        verified = json.loads(result.stdout)
        self.assertTrue(verified['actual_keystore_verified'])
        self.assertEqual(verified['certificate_sha256'], self.metadata['KSU_EXPECTED_HASH'])
        self.assertEqual(verified['package_name'], 'io.xkcnsu.manager')
        self.assertEqual(verified['key_alias'], 'xkcnsu-release')

    def test_kernel_arguments_are_derived_from_the_verified_certificate(self):
        result = self.verify('--kernel-args')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip().split(), [
            'io.xkcnsu.manager', self.metadata['KSU_EXPECTED_SIZE'], self.metadata['KSU_EXPECTED_HASH'],
        ])

    def test_wrong_requested_or_recorded_package_is_rejected(self):
        self.assertEqual(self.verify(package='io.other.manager').returncode, 1)
        self.metadata['KSU_PACKAGE_NAME'] = 'io.other.manager'
        result = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn('package', result.stderr)

    def test_stale_kernel_certificate_length_is_rejected(self):
        self.metadata['KSU_EXPECTED_SIZE'] = '0x0001'
        result = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn('size', result.stderr)

    def test_self_consistent_metadata_for_another_certificate_cannot_pass_the_real_keystore(self):
        certificate = (self.directory / 'different.der').read_bytes()
        (self.private / 'certificate.der').write_bytes(certificate)
        self.metadata.update(certificate_size=len(certificate),
                             KSU_EXPECTED_SIZE=f'0x{len(certificate):04x}',
                             KSU_EXPECTED_HASH=hashlib.sha256(certificate).hexdigest())
        result = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn('Actual keystore certificate differs', result.stderr)

    def test_stale_metadata_hash_is_rejected(self):
        self.metadata['KSU_EXPECTED_HASH'] = '00' * 32
        result = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn('SHA-256', result.stderr)

    def test_unusable_keystore_does_not_print_signing_secrets(self):
        (self.private / 'password').write_text('incorrect disposable password\n')
        result = self.verify()
        self.assertEqual(result.returncode, 1)
        self.assertIn('Actual keystore certificate differs', result.stderr)

    def test_signing_script_metadata_is_consumed_by_the_kernel_argument_verifier(self):
        root = self.private / 'project'
        private = root / 'out/private'
        private.mkdir(parents=True)
        (root / 'tools').mkdir()
        (root / 'kernel/manager').mkdir(parents=True)
        for name in ('build-signing.sh', 'build-common.sh'):
            shutil.copyfile(TOOLS / name, root / 'tools' / name)
        (root / 'kernel/manager/apk_sign.c').write_text('#define CERT_MAX_LENGTH 4096\n')
        shutil.copyfile(self.directory / 'primary.p12', private / 'xkcnsu-release.p12')
        shutil.copyfile(self.password_file, private / 'release-password')
        environment = dict(os.environ, SOURCE_DATE_EPOCH='1793933717',
                           XKCNSU_ROOT=str(root), XKCNSU_OUT=str(root / 'out'),
                           XKCNSU_CACHE=str(root / 'cache'), XKCNSU_TMPDIR=str(root / 'cache/tmp'),
                           XKCNSU_PACKAGE_NAME='io.xkcnsu.manager', XKCNSU_SIGNING_PROFILE='release')
        produced = subprocess.run(['bash', str(root / 'tools/build-signing.sh')],
                                  cwd=root, env=environment, capture_output=True, text=True, timeout=45)
        self.assertEqual(produced.returncode, 0, produced.stdout + produced.stderr)
        self.assertNotIn(self.password, produced.stdout + produced.stderr)
        verified = subprocess.run([
            sys.executable, str(TOOLS / 'verify-signing-identity.py'),
            '--metadata', str(private / 'certificate.json'), '--package', 'io.xkcnsu.manager',
            '--require-release', '--kernel-args',
        ], capture_output=True, text=True, timeout=45)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        package, size, digest = verified.stdout.strip().split()
        self.assertEqual(package, 'io.xkcnsu.manager')
        self.assertEqual(int(size, 16), len((self.directory / 'primary.der').read_bytes()))
        self.assertEqual(digest, hashlib.sha256((self.directory / 'primary.der').read_bytes()).hexdigest())


if __name__ == '__main__':
    unittest.main()
