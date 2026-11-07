#!/usr/bin/env python3
"""Verify one Manager/Kernel signing identity before compiling either side."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

from signing_config import SigningConfigError, load_signing_config


def verify(metadata_path: Path, package_name: str, *, require_release: bool = False) -> dict:
    config = load_signing_config(metadata_path, require_release=require_release)
    metadata = json.loads(config.metadata_file.read_text(encoding='utf-8'))
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+', package_name):
        raise SigningConfigError('Invalid requested Manager package')
    if metadata.get('package_name') != package_name or metadata.get('KSU_PACKAGE_NAME') != package_name:
        raise SigningConfigError('Manager package and kernel signing metadata do not match')
    certificate = config.certificate_der.read_bytes()
    expected_size = metadata.get('KSU_EXPECTED_SIZE')
    if not isinstance(expected_size, str) or not re.fullmatch(r'0x[0-9a-fA-F]+', expected_size):
        raise SigningConfigError('Kernel certificate size must be explicit hexadecimal metadata')
    if int(expected_size, 16) != len(certificate):
        raise SigningConfigError('Kernel certificate size differs from the signing certificate')
    try:
        exported = subprocess.run([
            'keytool', '-exportcert', '-keystore', str(config.keystore),
            '-alias', config.key_alias, '-storepass:file', str(config.password_file),
        ], capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise SigningConfigError('Cannot export the configured Manager keystore certificate') from None
    if exported.returncode != 0 or exported.stdout != certificate:
        raise SigningConfigError('Actual keystore certificate differs from kernel signing metadata')
    return {
        'package_name': package_name,
        'certificate_size': len(certificate),
        'certificate_sha256': hashlib.sha256(certificate).hexdigest(),
        'kernel_expected_size': expected_size,
        'key_alias': config.key_alias,
        'signing_profile': config.signing_profile,
        'actual_keystore_verified': True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--metadata', required=True, type=Path)
    parser.add_argument('--package', required=True, dest='package_name')
    parser.add_argument('--require-release', action='store_true')
    parser.add_argument('--kernel-args', action='store_true')
    args = parser.parse_args()
    try:
        result = verify(args.metadata, args.package_name, require_release=args.require_release)
    except (SigningConfigError, OSError, ValueError) as error:
        print(f'Signing identity verification failed: {error}', file=sys.stderr)
        return 1
    if args.kernel_args:
        print(result['package_name'], result['kernel_expected_size'], result['certificate_sha256'])
    else:
        print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
