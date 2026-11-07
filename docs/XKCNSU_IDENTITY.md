# XKCNSU identity and signing

XKCNSU starts from the upstream version identified in [UPSTREAM.md](../UPSTREAM.md).
The independent application identity is `io.xkcnsu.manager`.

## Matching identities

| Consumer | Authoritative input |
| --- | --- |
| Manager applicationId | `XKCNSU_PACKAGE_NAME` / Gradle `KSU_PACKAGE_NAME` |
| Manager signing plugin | Private Gradle init script generated from validated metadata |
| Kernel package authentication | `KSU_PACKAGE_NAME` from the same validated metadata |
| Kernel certificate byte length | `KSU_EXPECTED_SIZE` from the DER certificate |
| Kernel certificate SHA-256 | `KSU_EXPECTED_HASH` from the DER certificate |
| Final APK check | `apksigner verify --print-certs`, package/version and native asset verification |

The Kotlin, AIDL and JNI namespace stays `me.weishu.kernelsu`; the Android
applicationId is independent of that namespace. Renaming only one side of
native class bindings would break JNI registration.

The SHA-256 of the signing certificate differs from the SHA-256 of the APK file.
`KSU_EXPECTED_HASH` authenticates the DER signing certificate.

The private hidden-Manager identity record uses `XKHI` (`0x584b4849`) and its
refresh request uses `XKRF` (`0x584b5246`). The C UAPI, Rust consumers and Kotlin
record writer are changed together. Existing KernelSU ecosystem IOCTLs remain
unchanged; XKCNSU's private identity records require its matching kernel.

## Before building

The signing script creates an identity once and reuses it on subsequent runs.
The identity verifier exports the actual keystore certificate with `keytool`
and checks it against the public DER file and metadata. It verifies both
package fields and the kernel's hexadecimal certificate size.

The LKM stage consumes the verifier's three-field output directly. Manager
and repack stages run the same verifier before using the signing metadata.
The final APK verifier additionally checks the actual APK signer against the
certificate expected by the rebuilt kernel modules and their embedded assets.

## Independent release settings

- Default package: `io.xkcnsu.manager`
- Release alias: `xkcnsu-release`
- Development alias: `xkcnsu-dev`
- New certificate subject: `CN=XKCNSU`
- Release keystore: `out/private/xkcnsu-release.p12`
- Signing metadata: `out/private/certificate.json`
- GitHub secrets: `XKCNSU_RELEASE_KEYSTORE_B64`, `XKCNSU_RELEASE_KEYSTORE_PASSWORD`
- Update URL: `XKCNSU_UPDATE_URL`, empty until the independent repository is configured

Private keys and passwords are excluded from public source and release packages.
Keep the first production identity for later APK updates. Disposable test keys
used by the host tests must never become a production release identity.

An earlier Manager failure cannot be attributed conclusively to a certificate
mismatch without its actual APK, kernel binary and runtime diagnostics. This
fork's checks address that failure mode without removing certificate checks.
