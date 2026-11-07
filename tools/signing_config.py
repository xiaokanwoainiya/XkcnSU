#!/usr/bin/env python3
"""Load local signing metadata and generate private build configurations."""
import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile


PASSWORD_FILE_TOKEN = "__XKCNSU_PASSWORD_FILE__"
SUPPORTED_ABIS = {"arm64-v8a", "armeabi-v7a", "x86", "x86_64", "riscv64"}


class SigningConfigError(ValueError):
    pass


@dataclass(frozen=True, repr=False)
class SigningConfig:
    metadata_file: Path
    keystore: Path
    key_alias: str
    certificate_der: Path
    password_file: Path
    signing_profile: str
    development_only: bool

    @property
    def output_name(self) -> str:
        return "XKCNSU" if self.signing_profile == "release" else "XKCNSU-dev"

    @property
    def apk_name(self) -> str:
        return self.output_name + ".apk"


def _read_file(path: Path, label: str, limit: int) -> bytes:
    flags = os.O_RDONLY
    for name in ("O_NOFOLLOW", "O_CLOEXEC", "O_NONBLOCK", "O_BINARY"):
        flags |= getattr(os, name, 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
                raise SigningConfigError(f"{label} must be a bounded, nonempty regular file")
            data = stream.read(limit + 1)
    except OSError:
        raise SigningConfigError(f"Cannot read {label}") from None
    if not 0 < len(data) <= limit:
        raise SigningConfigError(f"{label} must be a bounded, nonempty regular file")
    return data


def _text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise SigningConfigError(f"Invalid {label}")
    return value


def _private_path(directory: Path, value, label: str) -> Path:
    value = _text(str(value) if isinstance(value, Path) else value, label)
    path = Path(value)
    if ".." in path.parts:
        raise SigningConfigError(f"{label} cannot contain parent traversal")
    if not path.is_absolute():
        path = directory / path
    try:
        relative = path.relative_to(directory)
    except ValueError:
        raise SigningConfigError(f"{label} must remain in the signing metadata directory") from None
    current = directory
    try:
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise SigningConfigError(f"{label} cannot use symbolic links")
        info = path.stat()
    except OSError:
        raise SigningConfigError(f"Missing {label}") from None
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
        raise SigningConfigError(f"{label} must be a nonempty regular file")
    return path


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SigningConfigError("Duplicate signing metadata field")
        result[key] = value
    return result


def load_signing_config(metadata_path, *, require_release: bool | None = None) -> SigningConfig:
    """Validate metadata without reading the keystore or password contents."""
    path = Path(metadata_path).absolute()
    try:
        directory = path.parent.resolve(strict=True)
    except OSError:
        raise SigningConfigError("Signing metadata directory is missing") from None
    path = _private_path(directory, directory / path.name, "signing metadata")
    try:
        metadata = json.loads(
            _read_file(path, "signing metadata", 65536).decode("utf-8"),
            object_pairs_hook=_unique_fields,
        )
    except (UnicodeError, json.JSONDecodeError):
        raise SigningConfigError("Invalid signing metadata JSON") from None
    if not isinstance(metadata, dict):
        raise SigningConfigError("Signing metadata must be an object")
    development_only = metadata.get("development_only")
    if type(development_only) is not bool:
        raise SigningConfigError("Signing metadata requires an explicit development_only boolean")
    profile = metadata.get("signing_profile")
    if profile is None and development_only is True:
        profile = "development"
    if profile not in ("release", "development"):
        raise SigningConfigError("Signing metadata requires a recognized signing_profile")
    if development_only != (profile == "development"):
        raise SigningConfigError("Signing profile and development_only disagree")
    if require_release is not None and type(require_release) is not bool:
        raise SigningConfigError("require_release must be a boolean")
    if (require_release or os.environ.get("XKCNSU_REQUIRE_RELEASE_SIGNING") == "1") and (
        metadata.get("signing_profile") != "release" or development_only is not False
    ):
        raise SigningConfigError("Explicit release signing metadata is required")

    keystore = _private_path(directory, metadata.get("keystore"), "keystore")
    alias = _text(metadata.get("key_alias"), "key alias")
    certificate_der = _private_path(
        directory, metadata.get("certificate_der"), "signing certificate"
    )
    password_file = _private_path(
        directory, metadata.get("password_file"), "signing password file"
    )
    if len({path, keystore, certificate_der, password_file}) != 4:
        raise SigningConfigError("Signing metadata and signing material must use distinct files")
    certificate = _read_file(certificate_der, "signing certificate", 65536)
    digest = metadata.get("KSU_EXPECTED_HASH")
    size = metadata.get("certificate_size")
    if (not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None or
            hashlib.sha256(certificate).hexdigest() != digest.lower()):
        raise SigningConfigError("Signing certificate does not match metadata SHA-256")
    if type(size) is not int or size != len(certificate):
        raise SigningConfigError("Signing certificate does not match metadata size")
    return SigningConfig(
        metadata_file=path, keystore=keystore, key_alias=alias,
        certificate_der=certificate_der, password_file=password_file,
        signing_profile=profile, development_only=development_only,
    )


def read_signing_password(config: SigningConfig) -> bytes:
    path = _private_path(
        config.metadata_file.parent, config.password_file, "signing password file"
    )
    data = _read_file(path, "signing password file", 16384)
    if data.endswith(b"\r\n"):
        data = data[:-2]
    elif data.endswith((b"\r", b"\n")):
        data = data[:-1]
    if not data or any(value < 32 or value == 127 for value in data):
        raise SigningConfigError("Signing password must contain one nonempty UTF-8 line")
    try:
        data.decode("utf-8")
    except UnicodeError:
        raise SigningConfigError("Signing password must contain one nonempty UTF-8 line") from None
    return data


def _groovy_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def gradle_init_script(config: SigningConfig) -> str:
    password = read_signing_password(config).decode("utf-8")
    properties = {
        "KEYSTORE_FILE": str(config.keystore),
        "KEYSTORE_PASSWORD": password,
        "KEY_ALIAS": config.key_alias,
        "KEY_PASSWORD": password,
    }
    lines = ["gradle.beforeProject { p ->"]
    lines += [
        f"  p.ext.set({_groovy_string(key)}, {_groovy_string(value)})"
        for key, value in properties.items()
    ]
    return "\n".join([*lines, "}", ""])


def repack_signing(config: SigningConfig) -> dict:
    return {
        "keystore_path": str(config.keystore),
        "key_alias": config.key_alias,
        "keystore_pass": PASSWORD_FILE_TOKEN,
        "key_pass": PASSWORD_FILE_TOKEN,
    }


def repack_config(config: SigningConfig, abis) -> dict:
    abis = list(abis)
    if not abis or any(abi not in SUPPORTED_ABIS for abi in abis) or len(set(abis)) != len(abis):
        raise SigningConfigError("Repack requires distinct, supported Android ABIs")
    return {
        "app_build_type": "release", "ksud_build_type": "release",
        "arch": abis, "output_name": config.output_name, "strip": False,
        "signing": repack_signing(config),
    }


def _write_private(config: SigningConfig, output_path, data: str) -> None:
    path = Path(output_path).absolute()
    directory = config.metadata_file.parent
    try:
        path.parent.relative_to(directory)
        if ".." in path.parts:
            raise ValueError
        current = directory
        for part in path.parent.relative_to(directory).parts:
            current /= part
            if current.is_symlink():
                raise ValueError
        if not path.parent.is_dir() or path.is_symlink():
            raise ValueError
    except ValueError:
        raise SigningConfigError("Generated signing configuration must stay in its private directory") from None
    if path in {config.metadata_file, config.keystore, config.certificate_der, config.password_file}:
        raise SigningConfigError("Generated configuration cannot overwrite signing material")
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".signing-config-", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            if hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        raise SigningConfigError("Cannot write private signing configuration") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_gradle_init(config: SigningConfig, output_path) -> None:
    _write_private(config, output_path, gradle_init_script(config))


def write_repack_config(config: SigningConfig, output_path, abis) -> None:
    _write_private(config, output_path, json.dumps(repack_config(config, abis), indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    gradle = commands.add_parser("gradle-init")
    gradle.add_argument("--output", required=True)
    repack = commands.add_parser("repack-config")
    repack.add_argument("--output", required=True)
    repack.add_argument("--abis", required=True, help="Whitespace-separated Android ABIs")
    commands.add_parser("apk-name")
    args = parser.parse_args()
    try:
        config = load_signing_config(args.metadata)
        if args.command == "gradle-init":
            write_gradle_init(config, args.output)
        elif args.command == "repack-config":
            write_repack_config(config, args.output, args.abis.split())
        else:
            print(config.apk_name)
    except SigningConfigError as error:
        parser.exit(1, f"Signing configuration rejected: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
