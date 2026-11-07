package me.weishu.kernelsu.data.system

import java.net.URI
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.longOrNull
import me.weishu.kernelsu.ui.util.module.LatestVersionInfo

object CoreUpdatePolicy {
    // Configure XKCNSU_UPDATE_URL when the independent release repository exists.
    const val LATEST_RELEASE_URL = ""
    private val stableVersion = Regex("v?((?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*))")
    private val stableApk = Regex(
        "^XKCNSU_v?((?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*))_" +
            "([1-9][0-9]*)(?:-(?:release|arm64-v8a|arm64-v8a-release))?\\.apk$"
    )

    fun url(configured: String): String? {
        val value = configured.trim()
        val uri = runCatching { URI(value) }.getOrNull() ?: return null
        return value.takeIf {
            uri.scheme == "https" && !uri.host.isNullOrBlank() && uri.rawUserInfo == null
        }
    }

    fun versionCode(assetName: String): Int? =
        Regex("^XKCNSU(?:_PR)?_.+_(\\d+)(?:-[A-Za-z0-9._-]+)?\\.apk$")
            .matchEntire(assetName)?.groupValues?.get(1)?.toIntOrNull()

    fun isDownloadable(
        update: LatestVersionInfo,
        installedVersionCode: Long,
        endpoint: String = LATEST_RELEASE_URL,
    ): Boolean = runCatching {
        val api = URI(url(endpoint) ?: return false)
        if (!api.host.equals("api.github.com", true) || api.port !in listOf(-1, 443) ||
            api.rawQuery != null || api.rawFragment != null
        ) return false
        val repository = Regex("^/repos/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/releases/latest$")
            .matchEntire(api.path) ?: return false
        val name = stableApk.matchEntire(update.assetName) ?: return false
        if (name.groupValues[1] != update.version ||
            name.groupValues[2].toIntOrNull() != update.versionCode ||
            update.versionCode.toLong() <= installedVersionCode ||
            update.versionCode > 2100000000 || update.assetSize <= 0
        ) return false
        if (update.assetSha256 != null &&
            !Regex("[0-9a-fA-F]{64}").matches(update.assetSha256)
        ) return false
        val owner = repository.groupValues[1]
        val repo = repository.groupValues[2]
        listOf(update.version, "v${update.version}").any { tag ->
            isReleaseAssetUrl(update.downloadUrl, owner, repo, tag, update.assetName)
        }
    }.getOrDefault(false)

    fun release(
        statusCode: Int,
        body: String,
        currentVersionCode: Int,
        endpoint: String = LATEST_RELEASE_URL,
        isPrBuild: Boolean = false,
    ): LatestVersionInfo {
        if (isPrBuild || statusCode != 200 || body.length > 2 * 1024 * 1024) return LatestVersionInfo()
        return runCatching {
            val api = URI(url(endpoint) ?: return LatestVersionInfo())
            if (!api.host.equals("api.github.com", true) || api.port !in listOf(-1, 443) ||
                api.rawQuery != null || api.rawFragment != null
            ) return LatestVersionInfo()
            val repository = Regex("^/repos/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/releases/latest$")
                .matchEntire(api.path) ?: return LatestVersionInfo()
            val owner = repository.groupValues[1]
            val repo = repository.groupValues[2]
            val release = Json.parseToJsonElement(body) as? JsonObject ?: return LatestVersionInfo()
            if (release.boolean("draft") != false || release.boolean("prerelease") != false) {
                return LatestVersionInfo()
            }
            val tag = release.text("tag_name") ?: return LatestVersionInfo()
            val version = stableVersion.matchEntire(tag)?.groupValues?.get(1) ?: return LatestVersionInfo()
            val assets = release["assets"] as? JsonArray ?: return LatestVersionInfo()
            assets.mapNotNull { entry ->
                val asset = entry as? JsonObject ?: return@mapNotNull null
                val name = asset.text("name") ?: return@mapNotNull null
                val match = stableApk.matchEntire(name) ?: return@mapNotNull null
                if (match.groupValues[1] != version || asset.text("state") != "uploaded") {
                    return@mapNotNull null
                }
                val code = match.groupValues[2].toIntOrNull() ?: return@mapNotNull null
                if (code <= currentVersionCode || code > 2100000000) return@mapNotNull null
                val size = asset.number("size")?.takeIf { it > 0 } ?: return@mapNotNull null
                val download = asset.text("browser_download_url") ?: return@mapNotNull null
                if (!isReleaseAssetUrl(download, owner, repo, tag, name)) return@mapNotNull null
                val digest = asset.text("digest")
                if (asset["digest"] != null && asset["digest"] != JsonNull && digest == null) {
                    return@mapNotNull null
                }
                val sha256 = digest?.removePrefix("sha256:")?.lowercase()
                if (digest != null && (!digest.startsWith("sha256:") ||
                        sha256 == null || !Regex("[0-9a-f]{64}").matches(sha256))
                ) return@mapNotNull null
                LatestVersionInfo(
                    versionCode = code,
                    downloadUrl = download,
                    changelog = release.text("body").orEmpty(),
                    version = version,
                    assetName = name,
                    assetSize = size,
                    assetSha256 = sha256,
                )
            }.maxWithOrNull(compareBy<LatestVersionInfo> { it.versionCode }.thenBy { it.assetName })
                ?: LatestVersionInfo()
        }.getOrDefault(LatestVersionInfo())
    }

    private fun isReleaseAssetUrl(url: String, owner: String, repo: String, tag: String, name: String): Boolean {
        val uri = runCatching { URI(url) }.getOrNull() ?: return false
        return uri.scheme == "https" && uri.host.equals("github.com", true) &&
            uri.port in listOf(-1, 443) && uri.rawUserInfo == null &&
            uri.rawQuery == null && uri.rawFragment == null &&
            uri.path == "/$owner/$repo/releases/download/$tag/$name"
    }

    private fun JsonObject.text(key: String): String? =
        (get(key) as? JsonPrimitive)?.takeIf { it.isString }?.content

    private fun JsonObject.boolean(key: String): Boolean? =
        (get(key) as? JsonPrimitive)?.takeUnless { it.isString }?.booleanOrNull

    private fun JsonObject.number(key: String): Long? =
        (get(key) as? JsonPrimitive)?.takeUnless { it.isString }?.longOrNull
}
