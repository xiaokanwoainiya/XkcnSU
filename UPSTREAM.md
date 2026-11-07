# XkcnSU upstream attribution

XkcnSU is derived from **MilkSU**, published in the **MikSU** repository.

- Repository: https://github.com/Xiaogugtfgdfhgrh/MikSU
- Baseline tag: `v0.2.3`
- Baseline commit: `99bfbe84e197a44e10fde247d6cd0714d895c5f7`
- Original application ID: `io.milksu.manager`
- Derivative Manager: `XkcnSU`, application ID `io.xkcnsu.manager`

XkcnSU is maintained independently. Root, module management, KPM, SUSFS and
other inherited implementations remain credited to their upstream authors.

## Inherited upstream sources

| Component | Source | Pinned commit |
| --- | --- | --- |
| KernelSU | https://github.com/tiann/KernelSU | `28db74f6f4692e97006ce9ebd8b19b9cc614b5b7` |
| KernelSU-Next hooks and SUSFS | https://github.com/KernelSU-Next/KernelSU-Next | `a6c81fa4a8b7341d0bc5c3e4507531459df871ae` |
| KPatch-Next | https://github.com/KernelSU-Next/KPatch-Next | `456744b29efb9989445463ab29e368fa59a103c4` |
| KPatch-Next-Module | https://github.com/KernelSU-Next/KPatch-Next-Module | `b5612adf4d4085a253cb0be30fa1be36e11d09db` |

The original licenses, SPDX notices, copyright statements, pinned source
commits and source hashes are retained. Current-tree destination hashes in the
provenance records describe the XkcnSU source tree.

Manager and userspace code retain GPL-3.0-or-later. The kernel and vendored
components retain their respective licenses as recorded alongside the sources.
See the component licenses and [modification notice](NOTICE.md).

## XkcnSU changes

- The Manager displays XkcnSU. The package, certificate subject, build variables
  and runtime paths use the dedicated XKCNSU identity.
- Existing KernelSU UAPI, JNI namespace and module ecosystem identifiers remain
  consistent with their consumers.
- Automatic updates default to disabled until the project's release URL is configured.
- Kernel and Manager builds verify the package and the actual keystore
  certificate against the shared public certificate metadata before building.
- A new vector X mark replaces the upstream product artwork.

See the [comparison with the upstream release](README.md#与上游-v023-的差异)
for module installation, downloads, temporary Root and signing changes.
