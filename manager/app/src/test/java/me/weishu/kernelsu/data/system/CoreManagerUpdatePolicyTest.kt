package me.weishu.kernelsu.data.system

import me.weishu.kernelsu.ui.util.module.LatestVersionInfo
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.security.MessageDigest
import java.util.concurrent.CancellationException

class CoreManagerUpdatePolicyTest {
    private val bytes = ByteArray(40_000) { (it % 251).toByte() }
    private val hash = MessageDigest.getInstance("SHA-256").digest(bytes)
        .joinToString("") { "%02x".format(it) }
    private val certificate = "a".repeat(64)
    private val installed = CoreApkIdentity("io.xkcnsu.manager", 32800, "0.2.0", setOf(certificate))
    private val candidate = installed.copy(versionCode = 32801, versionName = "0.2.1")
    private val release = LatestVersionInfo(
        versionCode = 32801,
        downloadUrl = "https://github.com/fixture-owner/XKCNSU/releases/download/v0.2.1/XKCNSU_0.2.1_32801.apk",
        version = "0.2.1",
        assetName = "XKCNSU_0.2.1_32801.apk",
        assetSize = bytes.size.toLong(),
        assetSha256 = hash,
    )

    private fun withFile(block: (File) -> Unit) {
        val directory = Files.createTempDirectory("manager-update-test").toFile()
        try {
            block(File(directory, "update.apk"))
        } finally {
            directory.deleteRecursively()
        }
    }

    @Test
    fun completeNewerApkWithInstalledSignerIsAccepted() {
        CoreManagerUpdatePolicy.validateIdentity(installed, candidate, release)
        CoreManagerUpdatePolicy.validateIdentity(
            installed,
            candidate.copy(signerSha256 = setOf(certificate.uppercase())),
            release.copy(assetSha256 = null),
        )
    }

    @Test
    fun matchingHashNeverAuthorizesAnIncorrectApkIdentity() {
        for (invalid in listOf(
            candidate.copy(packageName = "me.weishu.kernelsu"),
            candidate.copy(packageName = "io.xkcnsu.manager.pr"),
            candidate.copy(versionCode = 32800),
            candidate.copy(versionCode = 32802),
            candidate.copy(versionName = "0.2.0"),
            candidate.copy(versionName = null),
            candidate.copy(signerSha256 = setOf("b".repeat(64))),
            candidate.copy(signerSha256 = emptySet()),
            candidate.copy(debuggable = true),
            candidate.copy(testOnly = true),
            candidate.copy(split = true),
        )) assertThrows(IOException::class.java) {
            CoreManagerUpdatePolicy.validateIdentity(installed, invalid, release)
        }
    }

    @Test
    fun missingOrInvalidInstalledSignerFailsClosed() {
        for (signers in listOf(emptySet(), setOf("invalid"), setOf(""))) {
            assertThrows(IOException::class.java) {
                CoreManagerUpdatePolicy.validateIdentity(installed.copy(signerSha256 = signers), candidate, release)
            }
        }
    }

    @Test
    fun multipleSignersMustMatchTheEntireInstalledSet() {
        val second = "b".repeat(64)
        val current = installed.copy(signerSha256 = setOf(certificate, second))
        CoreManagerUpdatePolicy.validateIdentity(
            current, candidate.copy(signerSha256 = setOf(second, certificate)), release,
        )
        assertThrows(IOException::class.java) {
            CoreManagerUpdatePolicy.validateIdentity(current, candidate, release)
        }
        assertThrows(IOException::class.java) {
            CoreManagerUpdatePolicy.validateIdentity(
                installed, candidate.copy(signerSha256 = setOf(certificate, second)), release,
            )
        }
    }

    @Test
    fun alreadyInstalledVersionIsNotReinstalled() {
        assertThrows(IOException::class.java) {
            CoreManagerUpdatePolicy.validateIdentity(installed.copy(versionCode = 32801), candidate, release)
        }
    }

    @Test
    fun boundedDownloadChecksSizeDigestAndReadback() = withFile { file ->
        var progress = 0L
        val actual = CoreManagerUpdatePolicy.copyDownload(
            ByteArrayInputStream(bytes), file, release, onProgress = { progress = it },
        )
        assertEquals(hash, actual)
        assertEquals(bytes.size.toLong(), progress)
        assertEquals(hash, CoreManagerUpdatePolicy.verifyFile(file, release, actual))
    }

    @Test
    fun optionalDigestStillPinsTheExactDownloadedBytesForReadback() = withFile { file ->
        val noDigest = release.copy(assetSha256 = null)
        val actual = CoreManagerUpdatePolicy.copyDownload(ByteArrayInputStream(bytes), file, noDigest)
        assertEquals(hash, actual)
        assertEquals(hash, CoreManagerUpdatePolicy.verifyFile(file, noDigest, actual))
        val changed = bytes.copyOf().also { it[7] = (it[7] + 1).toByte() }
        file.writeBytes(changed)
        assertThrows(IOException::class.java) {
            CoreManagerUpdatePolicy.verifyFile(file, noDigest, actual)
        }
    }

    @Test
    fun truncatedAndOversizedDownloadsAreRemoved() = withFile { file ->
        for (input in listOf(bytes.copyOf(bytes.size - 1), bytes + byteArrayOf(1))) {
            assertThrows(IOException::class.java) {
                CoreManagerUpdatePolicy.copyDownload(ByteArrayInputStream(input), file, release)
            }
            assertFalse(file.exists())
        }
    }

    @Test
    fun badDownloadDigestIsRemoved() = withFile { file ->
        assertThrows(IOException::class.java) {
            CoreManagerUpdatePolicy.copyDownload(
                ByteArrayInputStream(bytes), file, release.copy(assetSha256 = "b".repeat(64)),
            )
        }
        assertFalse(file.exists())
    }

    @Test
    fun invalidSizeAndMalformedDigestAreRejectedBeforeReading() = withFile { file ->
        for (invalid in listOf(
            release.copy(assetSize = 0),
            release.copy(assetSize = -1),
            release.copy(assetSize = CoreManagerUpdatePolicy.MAX_APK_BYTES + 1),
            release.copy(assetSha256 = "bad"),
        )) {
            assertThrows(IOException::class.java) {
                CoreManagerUpdatePolicy.copyDownload(ByteArrayInputStream(bytes), file, invalid)
            }
            assertFalse(file.exists())
        }
    }

    @Test
    fun cancellationRemovesPartialDownload() = withFile { file ->
        var checks = 0
        assertThrows(CancellationException::class.java) {
            CoreManagerUpdatePolicy.copyDownload(
                ByteArrayInputStream(bytes), file, release,
                checkActive = { if (++checks == 3) throw CancellationException("cancelled") },
            )
        }
        assertFalse(file.exists())
    }

    @Test
    fun readbackRejectsTruncatedOrChangedSavedFile() = withFile { file ->
        file.writeBytes(bytes.copyOf(bytes.size - 1))
        assertThrows(IOException::class.java) { CoreManagerUpdatePolicy.verifyFile(file, release) }
        file.writeBytes(bytes.copyOf().also { it[0] = 99 })
        assertThrows(IOException::class.java) { CoreManagerUpdatePolicy.verifyFile(file, release) }
    }

    @Test
    fun downloadEntryRechecksOfferInsteadOfTrustingCallerMetadata() {
        assertTrue(CoreUpdatePolicy.isDownloadable(release, 32800, "https://api.github.com/repos/fixture-owner/XKCNSU/releases/latest"))
        for (invalid in listOf(
            release.copy(versionCode = 32800),
            release.copy(version = "0.2.0"),
            release.copy(assetName = "../update.apk"),
            release.copy(assetName = "XKCNSU_PR_0.2.1_32801.apk"),
            release.copy(downloadUrl = release.downloadUrl.replace("/XKCNSU/", "/KernelSU/")),
            release.copy(assetSize = 0),
            release.copy(assetSha256 = "invalid"),
        )) assertFalse(CoreUpdatePolicy.isDownloadable(invalid, 32800, "https://api.github.com/repos/fixture-owner/XKCNSU/releases/latest"))
    }
}
