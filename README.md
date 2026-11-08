# XKCNSU

<img src="manager/artwork/xkcnsu.svg" width="112" height="112" alt="XKCNSU">

XKCNSU 是基于 [MilkSU（MikSU）](https://github.com/Xiaogugtfgdfhgrh/MikSU)
v0.2.3 的独立衍生项目。Root、模块、KPM、SUSFS 等继承功能来自上游，
完整来源和固定版本见 [UPSTREAM.md](UPSTREAM.md)。本项目保留原作者版权与许可证。

## 当前开发基线

| 项目 | 当前值 |
| --- | --- |
| 应用名称 | XKCNSU |
| applicationId | `io.xkcnsu.manager` |
| 内部 Kotlin / JNI / AIDL 命名空间 | `me.weishu.kernelsu`，与现有调用链保持一致 |
| 管理器版本 | 0.2.3 / 32803，继承基线版本号 |
| 内核版本码 | 32802，继承基线版本号 |
| 私有项目状态目录 | `/data/adb/ksu/XKCNSU/` |
| 正式签名别名 | `xkcnsu-release` |
| 证书主题 | `CN=XKCNSU` |
| 默认自动更新 | 关闭，等待配置独立仓库 |

本次源码完成产品名称、运行标识和签名构建链路调整。完整 Android APK、
内核编译和设备运行状态以相应构建及真机验证结果为准。

## 继承功能

- 按应用 Root 授权、应用配置、UID/GID、权能与 SELinux 上下文。
- 模块安装、启停、更新、卸载、WebUI 和 metamodule。
- KPM 模块导入、加载、控制、开机加载与状态查询。
- SUSFS 集成与对应用户态工具接口。
- Miuix / Material 两套 Compose 界面。

Root 内核和内置 KPM 引擎面向 ARM64 GKI。管理器包含 ARM64 / x86_64 构建配置。
实际兼容性需要结合 KMI、厂商内核配置和设备验证。

## 签名与内核认证

管理器 APK 签名证书、内核认证参数和发布构建必须来自同一份正式签名身份。
不要直接使用上游已编译的内核模块配合重新签名的 XKCNSU APK。

首次准备正式身份，之后持续复用同一份密钥：

```sh
bash tools/build-signing.sh
python3 tools/verify-signing-identity.py \
  --metadata out/private/certificate.json \
  --package io.xkcnsu.manager --require-release
```

密钥、口令文件和 `certificate.json` 位于私有构建目录，私钥不属于公开源码。
构建核对实际密钥库导出的 DER 证书、证书 SHA-256、长度和包名，
从 `certificate.json` 派生 `KSU_PACKAGE_NAME`、`KSU_EXPECTED_SIZE` 和
`KSU_EXPECTED_HASH`。任一不一致即停止对应构建阶段。

后续只更新 APK 时继续使用原正式密钥；变更证书、包名或内核认证参数后，
需要成套重编译和安装匹配的组件。完整说明见
[XKCNSU_IDENTITY.md](docs/XKCNSU_IDENTITY.md)。

## 从源码构建

在已配置 Git 的源码检出目录中使用 Linux / WSL Ubuntu：

```sh
bash tools/bootstrap-xkcnsu.sh all
bash tools/bootstrap-kpatch-toolchain.sh
bash tools/build-all.sh --source-ready
```

缓存默认位于 `/opt/xkcnsu`，产物位于 `out/`。构建需要 Android、Rust、
内核工具链及对应依赖。GitHub 工作流使用自己的
`XKCNSU_RELEASE_KEYSTORE_B64` 与 `XKCNSU_RELEASE_KEYSTORE_PASSWORD`。

- [管理器说明](manager/README.xkcnsu.md)
- [构建与维护](docs/XKCNSU_TECHNICAL.md)
- [手动构建工作流](.github/workflows/xkcnsu-build.yml)

## 独立更新源

创建 XKCNSU 仓库后，将 Gradle 属性或环境变量 `XKCNSU_UPDATE_URL` 设置为：

```text
https://api.github.com/repos/OWNER/XKCNSU/releases/latest
```

将 `OWNER` 替换为实际账号。默认值为空，不会查询或下载上游管理器更新。
正式附件名称使用 `XKCNSU_版本名_版本码.apk`，并持续复用正式签名。

## 目录与许可证

`manager/`、`kernel/`、`userspace/`、`uapi/`、`vendor/`、`tools/` 保留完整源码。
管理器和用户态沿用 [GPL-3.0-or-later](LICENSE)，内核沿用
[GPL-2.0](kernel/LICENSE)，其他组件保留各自原许可证、版权与来源记录。
