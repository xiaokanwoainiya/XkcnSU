# XkcnSU

<img src="manager/artwork/xkcnsu.svg" width="112" height="112" alt="XkcnSU">

XkcnSU 是基于 MilkSU v0.2.3 的独立 Android Root 管理器。Root 授权、模块管理、KPM
和 SUSFS 等功能继承自上游，原作者版权、许可证与来源记录随源码保留。
[上游 MikSU 仓库](https://github.com/Xiaogugtfgdfhgrh/MikSU) · [来源与固定版本](UPSTREAM.md)

## 当前开发基线

| 项目 | 当前值 |
| --- | --- |
| 应用名称 | XkcnSU |
| applicationId | `io.xkcnsu.manager` |
| 内部 Kotlin / JNI / AIDL 命名空间 | `me.weishu.kernelsu`，与现有调用链保持一致 |
| 管理器版本 | 0.2.3 / 32803，继承基线版本号 |
| 内核版本码 | 32802，继承基线版本号 |
| 私有项目状态目录 | `/data/adb/ksu/XKCNSU/` |
| 正式签名别名 | `xkcnsu-release` |
| 证书主题 | `CN=XKCNSU` |
| 默认自动更新 | 关闭，配置独立发布地址后启用 |

## 与上游 v0.2.3 的差异

对比基线为上游提交 `99bfbe84e197a44e10fde247d6cd0714d895c5f7`。

| 范围 | 上游基线 | XkcnSU |
| --- | --- | --- |
| 产品标识 | 原管理器名称、包名和图标 | 独立显示名、包名 `io.xkcnsu.manager`、图标及发布渠道 |
| 模块即时安装 | 由模块安装器显式请求热安装 | 安装后默认尝试运行 `service.sh`，成功即时激活时清除待更新标记。发现 `system.prop` 或 `post-fs-data.sh` 任一文件则保留待重启状态 |
| 模块下载 | 单连接下载 | 模块更新和云端模块支持最多 16 路 HTTP Range 下载，服务器或文件条件不满足时回退单连接 |
| 临时 Root | 与镜像修补共享部分 KPM 和 SUSFS 初始化路径 | 临时 Root 入口使用基础 Root 模块，跳过 KPM 和 SUSFS 的开关与启动分派。镜像修补保留对应功能 |
| 成品校验 | 核对包名及证书 SHA-256 | 额外要求 APK 仅通过 V2 验签且只有一个签名者，证书哈希与配套内核的编译输入一致 |

模块的开机挂载、系统属性和 `post-fs-data.sh` 仍在相应启动阶段生效。
[上游基线源码](https://github.com/Xiaogugtfgdfhgrh/MikSU/tree/99bfbe84e197a44e10fde247d6cd0714d895c5f7)

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
重新签名后，请使用以同一证书编译的 XkcnSU 内核模块。

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
内核工具链及对应依赖。GitHub 工作流的 `release` 模式使用仓库配置的
`XKCNSU_RELEASE_KEYSTORE_B64` 和 `XKCNSU_RELEASE_KEYSTORE_PASSWORD`。
选择 `development` 可为本次测试构建生成一套开发证书，APK 和内核模块成套使用。

- [管理器说明](manager/README.xkcnsu.md)
- [构建与维护](docs/XKCNSU_TECHNICAL.md)
- [手动构建工作流](.github/workflows/xkcnsu-build.yml)

## 独立更新源

发布配套 APK 后，可将 Gradle 属性或环境变量 `XKCNSU_UPDATE_URL` 指向独立仓库的发布 API。

```text
https://api.github.com/repos/xiaokanwoainiya/XkcnSU/releases/latest
```

默认值为空。发布附件继续使用 `XKCNSU_版本名_版本码.apk` 的命名约定，并复用正式签名。
[XkcnSU 仓库](https://github.com/xiaokanwoainiya/XkcnSU)

## 目录与许可证

`manager/`、`kernel/`、`userspace/`、`uapi/`、`vendor/`、`tools/` 保留完整源码。
管理器和用户态沿用 GPL-3.0-or-later，内核沿用 GPL-2.0，各组件保留各自的版权声明。
[许可与修改声明](NOTICE.md) · [GPL v3 正文](LICENSE) · [内核许可](kernel/LICENSE)
