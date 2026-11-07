package me.weishu.kernelsu.data.system

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import me.weishu.kernelsu.ui.util.module.LatestVersionInfo
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class CoreUpdatePolicyTest {
    private val endpoint = "https://api.github.com/repos/fixture-owner/XKCNSU/releases/latest"
    private val assetName = "XKCNSU_0.2.1_32801.apk"
    private val downloadRoot = "https://github.com/fixture-owner/XKCNSU/releases/download"
    private val sha256 = "a".repeat(64)

    private fun asset(
        name: String = assetName,
        tag: String = "v0.2.1",
        url: String = "$downloadRoot/$tag/$name",
        state: String = "uploaded",
        size: Long = 1024,
        digest: String? = "sha256:$sha256",
    ): JsonObject = buildJsonObject {
        put("name", name)
        put("state", state)
        put("size", size)
        put("browser_download_url", url)
        digest?.let { put("digest", it) }
    }

    private fun release(
        assets: List<JsonElement> = listOf(asset()),
        tag: String = "v0.2.1",
        draft: Boolean = false,
        prerelease: Boolean = false,
    ): String = buildJsonObject {
        put("tag_name", tag)
        put("draft", draft)
        put("prerelease", prerelease)
        put("body", "Actual release notes")
        put("assets", JsonArray(assets))
    }.toString()

    private fun select(body: String = release(), code: Int = 32800) =
        CoreUpdatePolicy.release(200, body, code, endpoint)

    @Test
    fun defaultEndpointStaysUnconfiguredUntilTheForkHasItsOwnRepository() {
        assertEquals(
            "",
            CoreUpdatePolicy.LATEST_RELEASE_URL,
        )
    }

    @Test
    fun newerUploadedStableApkProvidesOnlyPublishedMetadata() {
        val result = select()
        assertEquals(32801, result.versionCode)
        assertEquals("0.2.1", result.version)
        assertEquals(assetName, result.assetName)
        assertEquals("$downloadRoot/v0.2.1/$assetName", result.downloadUrl)
        assertEquals("Actual release notes", result.changelog)
        assertEquals(1024L, result.assetSize)
        assertEquals(sha256, result.assetSha256)
    }

    @Test
    fun noReleaseAndHttpFailuresNeverOfferAnUpdate() {
        for ((status, body) in listOf(
            404 to """{"message":"Not Found"}""",
            403 to """{"message":"API rate limit exceeded"}""",
            429 to "rate limited",
            500 to "<html>failure</html>",
            204 to "",
        )) {
            assertEquals(LatestVersionInfo(), CoreUpdatePolicy.release(status, body, 32800))
        }
    }

    @Test
    fun sourcePushAndSourceOnlyReleaseNeverOfferAnUpdate() {
        for (body in listOf(
            """{"ref":"refs/heads/main","after":"source-commit"}""",
            release(assets = emptyList()),
            release(assets = listOf(asset(name = "XKCNSU-source.zip"))),
        )) assertEquals(LatestVersionInfo(), select(body))
    }

    @Test
    fun draftPrereleaseAndPrManagersNeverOfferStableUpdates() {
        assertEquals(LatestVersionInfo(), select(release(draft = true)))
        assertEquals(LatestVersionInfo(), select(release(prerelease = true)))
        assertEquals(
            LatestVersionInfo(),
            CoreUpdatePolicy.release(200, release(), 32800, endpoint, isPrBuild = true),
        )
        assertEquals(LatestVersionInfo(), select(release(tag = "v0.2.1-rc1")))
    }

    @Test
    fun currentAndOlderVersionCodesAreNotUpdates() {
        assertEquals(LatestVersionInfo(), select(code = 32801))
        assertEquals(LatestVersionInfo(), select(code = 40000))
    }

    @Test
    fun stableGradleAndArm64AssetNamesAreAccepted() {
        for (name in listOf(
            assetName,
            "XKCNSU_v0.2.1_32801-release.apk",
            "XKCNSU_0.2.1_32801-arm64-v8a.apk",
            "XKCNSU_0.2.1_32801-arm64-v8a-release.apk",
        )) assertEquals(name, select(release(listOf(asset(name = name)))).assetName)
    }

    @Test
    fun developmentUnsignedPrAndUpstreamArtifactsAreIgnored() {
        for (name in listOf(
            "XKCNSU-dev.apk",
            "XKCNSU_PR_0.2.1_32801.apk",
            "XKCNSU_0.2.1_32801-debug.apk",
            "XKCNSU_0.2.1_32801-repack-unsigned.apk",
            "XKCNSU_0.2.1_32801-repack-aligned.apk",
            "XKCNSU_0.2.1_32801-x86_64.apk",
            "KernelSU_0.2.1_32801-release.apk",
            "XKCNSU_0.2.1-dev_32801.apk",
            "XKCNSU_0.2.0_32801.apk",
        )) assertEquals(LatestVersionInfo(), select(release(listOf(asset(name = name)))))
    }

    @Test
    fun wrongRepositoryTagOrAssetUrlCannotBeOffered() {
        val correct = "$downloadRoot/v0.2.1/$assetName"
        for (url in listOf(
            correct.replace("/XKCNSU/", "/KernelSU/"),
            correct.replace("/fixture-owner/", "/another-owner/"),
            correct.replace("/v0.2.1/", "/v0.2.0/"),
            correct.replace(assetName, "another.apk"),
            correct.replace("https:", "http:"),
            correct.replace("github.com/", "github.com.attacker.invalid/"),
            correct.replace("github.com/", "user@github.com/"),
            "$correct?token=value",
            "$correct#fragment",
        )) assertEquals(LatestVersionInfo(), select(release(listOf(asset(url = url)))))
    }

    @Test
    fun partialEmptyAndOversizedCodeAssetsAreIgnored() {
        for (item in listOf(
            asset(state = "new"),
            asset(size = 0),
            asset(size = -1),
            asset(name = "XKCNSU_0.2.1_2147483648.apk"),
            asset(name = "XKCNSU_0.2.1_2100000001.apk"),
            asset(name = "XKCNSU_0.2.1_0.apk"),
        )) assertEquals(LatestVersionInfo(), select(release(listOf(item))))
    }

    @Test
    fun allAssetsAreExaminedAndHighestValidVersionCodeWins() {
        val body = release(listOf(
            JsonPrimitive("bad asset"),
            asset(name = "XKCNSU_0.2.1_32801-debug.apk"),
            asset(name = "XKCNSU_0.2.1_32801-release.apk"),
            asset(name = "XKCNSU_0.2.1_32803-release.apk"),
            asset(name = "XKCNSU_0.2.1_32802.apk"),
        ))
        assertEquals(32803, select(body).versionCode)
    }

    @Test
    fun optionalGithubDigestIsValidatedWhenPresent() {
        assertEquals(32801, select(release(listOf(asset(digest = null)))).versionCode)
        assertEquals(
            sha256,
            select(release(listOf(asset(digest = "sha256:${sha256.uppercase()}")))).assetSha256,
        )
        for (digest in listOf("sha1:$sha256", "sha256:bad", "", "sha256:${"z".repeat(64)}")) {
            assertEquals(LatestVersionInfo(), select(release(listOf(asset(digest = digest)))))
        }
    }

    @Test
    fun malformedOrWronglyTypedResponsesDoNotInventAnUpdate() {
        val wrongSize = JsonObject(asset().toMutableMap().apply {
            put("size", JsonPrimitive("1024"))
        })
        val wrongDigest = JsonObject(asset().toMutableMap().apply {
            put("digest", JsonPrimitive(123))
        })
        for (body in listOf(
            "{", "null", "[]", "{}", "<html>error</html>",
            release(listOf(wrongSize)),
            release(listOf(wrongDigest)),
            release().replace("\"draft\":false", "\"draft\":\"false\""),
            " ".repeat(2 * 1024 * 1024 + 1),
        )) assertEquals(LatestVersionInfo(), select(body))
    }

    @Test
    fun endpointMustBeAnExplicitGithubLatestReleaseApi() {
        for (endpoint in listOf(
            "", "http://api.github.com/repos/fixture-owner/XKCNSU/releases/latest",
            "https://api.github.com/repos/fixture-owner/XKCNSU/commits",
            "https://api.github.com/repos/fixture-owner/XKCNSU/releases/latest?token=secret",
            "https://user:password@api.github.com/repos/fixture-owner/XKCNSU/releases/latest",
            "https://example.com/latest",
        )) {
            assertEquals(
                LatestVersionInfo(),
                CoreUpdatePolicy.release(200, release(), 32800, endpoint),
            )
        }
        assertEquals(LatestVersionInfo(), CoreUpdatePolicy.release(200, release(), 32800))
        assertTrue(select().downloadUrl.startsWith("$downloadRoot/"))
    }
}
