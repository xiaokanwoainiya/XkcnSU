# XKCNSU Build And Maintenance

[Project overview](../README.md) | [Manager documentation](../manager/README.xkcnsu.md)

## Build Environment

Use Linux or WSL Ubuntu with Git, Python, Docker and network access.
Toolchains and caches default to `/opt/xkcnsu`; build outputs are written to
`out/`. The scripts in `tools/` provision the pinned Android, Rust, kernel and
KPatch toolchains.

```sh
bash tools/bootstrap-xkcnsu.sh all
bash tools/bootstrap-kpatch-toolchain.sh
bash tools/build-all.sh --source-ready
```

The build order is kernel modules and native tools, ksuinit, ksud, and then
the Android Manager. The final APK must contain the corresponding native
assets for each supported KMI.

Individual build stages are available through:

```sh
bash tools/build-release.sh snapshot --source-ready
bash tools/build-release.sh ksuinit
bash tools/build-release.sh ksud
bash tools/build-release.sh manager
bash tools/build-release.sh repack
```

Build the kernel and KPatch assets before invoking those stages. See
[`build-all.sh`](../tools/build-all.sh) for the complete sequence and
[`build-common.sh`](../tools/build-common.sh) for environment settings.

## Versioning

`version.properties` defines the Manager, daemon and kernel versions:

```properties
XKCNSU_VERSION_NAME=0.2.0
XKCNSU_VERSION_CODE=32800
```

Increase both values for a new release. Documentation-only source changes
do not require a new APK version.

## Signing

Official builds use:

```sh
export XKCNSU_SIGNING_PROFILE=release
export XKCNSU_REQUIRE_RELEASE_SIGNING=1
bash tools/build-signing.sh
```

The release keystore is `out/private/xkcnsu-release.p12`, with alias
`xkcnsu-release`. Its password is read from `out/private/release-password`.
`out/private/certificate.json` supplies the selected certificate and signing
configuration to the build tools.

The APK signer must match the certificate parameters compiled into the root
kernel modules. Future APK updates must use the same signing identity.

The manual GitHub build workflow reads `XKCNSU_RELEASE_KEYSTORE_B64` and
`XKCNSU_RELEASE_KEYSTORE_PASSWORD` from repository secrets. Private signing
files are excluded from source and public artifact packages.

## Validation

For Rust changes, run the Android target checks and formatting:

```sh
bash tools/build-rust.sh android-check
bash tools/build-rust.sh android-clippy
bash tools/build-rust.sh format-check
bash tools/build-rust.sh host-tests
```

The Manager stage runs Android unit tests, lint and the release build:

```sh
bash tools/build-release.sh manager
```

Build and signing regression tests run on temporary fixtures:

```sh
python3 -m unittest discover -s tools/tests -p 'test_*.py'
python3 -m unittest discover -s tools -p test_package_release.py
python3 -m unittest discover -s tools -p test_root_policy_acceptance.py
python3 tools/audit-public-source.py
```

The final APK verifier checks package identity, version, signing
certificate, installation attributes, embedded daemon and native asset
hashes. Host checks do not establish compatibility with every vendor kernel.

## Runtime Interfaces

- Root control uses the KernelSU UAPI, application profiles and allowlist.
- `ksud kpm` returns a JSON envelope containing `ok`, `data` and `error`.
- `ksud susfs status` returns the SUSFS backend status as a JSON object.
- SUSFS mutation commands use their exit status and diagnostic output.
- KPM and SUSFS operations are serialized and query the backend afterward.

The Manager, daemon and kernel interfaces must remain consistent when adding
commands or changing profile structures.

## Storage

| Purpose | Path |
| --- | --- |
| Daemon | `/data/adb/ksud` |
| Core state | `/data/adb/ksu/` |
| XKCNSU state | `/data/adb/ksu/XKCNSU/` |
| KPM registry and integration state | `/data/adb/ksu/XKCNSU/kpm/` |
| Ordinary modules | `/data/adb/modules/` |

## Image Transactions

KPM changes the kernel in `boot`. The root ramdisk can be located in `boot`,
`init_boot` or a selected vendor ramdisk, depending on the device.

Image transactions verify the input partition, backups and candidate image
before writing, then verify the partition contents after writing. Recovery
journals track transaction state.

Disabling KPM restores its saved original kernel while retaining the root
ramdisk. Root removal uses the corresponding image restoration process and
requires a reboot. Kernel code that cannot be proven quiescent remains
resident until reboot.

## Publishing

Publish stable GitHub Releases using a version tag such as `v0.2.1` and an
APK named `XKCNSU_0.2.1_32801.apk`. The version code must be higher than the
installed version for the Manager to offer an update.

Update APKs must retain the application ID and signing identity.
`SHA256SUMS` provides a checksum for manual download verification.

The build workflow is manually triggered and does not publish Releases
automatically. Public artifact packaging excludes partition images, private
keys, passwords and raw device evidence.

## Source Provenance

| Component | Pinned Source Commit |
| --- | --- |
| KernelSU | `28db74f6f4692e97006ce9ebd8b19b9cc614b5b7` |
| KernelSU-Next hooks and SUSFS | `a6c81fa4a8b7341d0bc5c3e4507531459df871ae` |
| KPatch-Next | `456744b29efb9989445463ab29e368fa59a103c4` |
| KPatch-Next-Module | `b5612adf4d4085a253cb0be30fa1be36e11d09db` |

Component licenses, copyright notices and provenance records are retained
alongside their sources. Upstream links are listed in the
[project overview](../README.md#上游项目).
