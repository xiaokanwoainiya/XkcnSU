plugins {
    alias(libs.plugins.agp.app) apply false
    alias(libs.plugins.kotlin) apply false
    alias(libs.plugins.compose.compiler) apply false
}

extra["androidMinSdkVersion"] = 31
extra["androidTargetSdkVersion"] = 37
extra["androidCompileSdkVersion"] = 37
extra["androidCompileSdkVersionMinor"] = 0
extra["androidBuildToolsVersion"] = "37.0.0"
extra["androidCompileNdkVersion"] = libs.versions.ndk.get()
extra["androidSourceCompatibility"] = JavaVersion.VERSION_21
extra["androidTargetCompatibility"] = JavaVersion.VERSION_21
// Release versions do not change when source-only commits are pushed.
val releaseVersion = java.util.Properties().apply {
    rootProject.file("../version.properties").inputStream().use { load(it) }
}
val bundledKernelVersionCode = requireNotNull(releaseVersion.getProperty("XKCNSU_VERSION_CODE")?.toIntOrNull()) {
    "version.properties must define XKCNSU_VERSION_CODE"
}
val stableVersionCode = releaseVersion.getProperty("XKCNSU_MANAGER_VERSION_CODE")?.toIntOrNull()
    ?: bundledKernelVersionCode
val stableVersionName = requireNotNull(releaseVersion.getProperty("XKCNSU_VERSION_NAME")) {
    "version.properties must define XKCNSU_VERSION_NAME"
}
val configuredVersionCode = providers.gradleProperty("XKCNSU_MANAGER_VERSION_CODE")
    .orElse(providers.environmentVariable("XKCNSU_MANAGER_VERSION_CODE"))
    .orElse(stableVersionCode.toString()).get().trim()
val configuredVersionName = providers.gradleProperty("XKCNSU_VERSION_NAME")
    .orElse(providers.environmentVariable("XKCNSU_VERSION_NAME"))
    .orElse(stableVersionName).get().trim()
val managerVersionCode = requireNotNull(configuredVersionCode.toIntOrNull()) {
    "XKCNSU_MANAGER_VERSION_CODE must be an integer"
}
require(managerVersionCode in stableVersionCode..2100000000) {
    "XKCNSU_VERSION_CODE must be at least $stableVersionCode and never decrease between releases"
}
require(managerVersionCode >= 32800 && managerVersionCode == stableVersionCode &&
    configuredVersionName == stableVersionName
) {
    "Manager version must match version.properties"
}
require(Regex("(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)").matches(configuredVersionName)) {
    "XKCNSU_VERSION_NAME must be a stable major.minor.patch version"
}
extra["managerVersionCode"] = managerVersionCode
extra["managerVersionName"] = configuredVersionName
extra["bundledKernelVersionCode"] = bundledKernelVersionCode
