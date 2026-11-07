#!/usr/bin/env python3
"""Run the repository repacker with password-file providers and no secret argv."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from signing_config import (
    PASSWORD_FILE_TOKEN,
    SigningConfigError,
    load_signing_config,
    read_signing_password,
    repack_signing,
)


def main() -> int:
    source = Path(sys.argv.pop(1)).resolve()
    out = Path(os.environ.get("XKCNSU_OUT", source / "out"))
    try:
        signing = load_signing_config(out / "private/certificate.json")
    except SigningConfigError as error:
        raise SystemExit(f"Signing configuration rejected: {error}") from None
    password_file = signing.password_file
    spec = importlib.util.spec_from_file_location("xkcnsu_repack", source / "repack_apk.py")
    if spec is None or spec.loader is None:
        raise SystemExit("Cannot load the repository APK repacker")
    repacker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(repacker)
    original_merge_config = repacker.merge_config

    def merge_config(file_config, arguments):
        config = original_merge_config(file_config, arguments)
        config["signing"] = repack_signing(signing)
        config["output_name"] = signing.output_name
        return config

    def run_command(arguments, fail_message):
        # apksigner caches password-file readers and consumes a line per option.
        # Give each option its own file so a shared password is read twice.
        with tempfile.TemporaryDirectory(prefix="apksigner-", dir=password_file.parent) as temporary:
            safe_arguments = []
            password = None
            for index, item in enumerate(arguments):
                if item == "pass:" + PASSWORD_FILE_TOKEN:
                    secret = Path(temporary) / f"password-{index}"
                    if password is None:
                        password = read_signing_password(signing)
                    descriptor = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(password + b"\n")
                    safe_arguments.append(f"file:{secret}")
                else:
                    safe_arguments.append(item)
            result = subprocess.run(
                safe_arguments, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
        if result.returncode:
            output = result.stdout
            if password is not None:
                output = output.replace(password.decode("utf-8"), "<redacted>")
            raise RuntimeError(f"{fail_message}\n{output.strip()}")

    repacker.merge_config = merge_config
    repacker.run_cmd = run_command
    return repacker.main()


if __name__ == "__main__":
    sys.exit(main())
