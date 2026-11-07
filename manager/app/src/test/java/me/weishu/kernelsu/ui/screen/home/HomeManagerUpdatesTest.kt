package me.weishu.kernelsu.ui.screen.home

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.runBlocking
import me.weishu.kernelsu.data.system.CoreUpdatePolicy
import me.weishu.kernelsu.ui.util.module.LatestVersionInfo
import org.junit.Assert.*
import org.junit.Test

class HomeManagerUpdatesTest {
    private val endpoint = "https://api.github.com/repos/fixture-owner/XKCNSU/releases/latest"
    private val release = LatestVersionInfo(
        versionCode = 32801,
        downloadUrl = "https://github.com/fixture-owner/XKCNSU/releases/download/v0.2.1/XKCNSU_0.2.1_32801.apk",
        changelog = "Actual release notes\nSecond line",
        version = "0.2.1",
        assetName = "XKCNSU_0.2.1_32801.apk",
        assetSize = 45_678_901L,
        assetSha256 = "a".repeat(64),
    )

    @Test fun completePendingMetadataSurvivesStateRecreation() {
        val saved = HomeManagerUpdates.savePending(release)
        assertTrue(saved.all { it is Int || it is Long || it is String })
        assertEquals(release.copy(changelog = ""), HomeManagerUpdates.restorePending(saved.toList()))
        assertEquals(release.copy(changelog = "", assetSha256 = null), HomeManagerUpdates.restorePending(
            HomeManagerUpdates.savePending(release.copy(assetSha256 = null)),
        ))
    }

    @Test fun missingOrMalformedSavedStateDoesNotCrashRestoration() {
        assertNull(HomeManagerUpdates.restorePending(HomeManagerUpdates.savePending(null)))
        val saved = HomeManagerUpdates.savePending(release)
        for (invalid in listOf(
            saved.dropLast(1), saved + "extra", saved.toMutableList().also { it[0] = 9 },
            saved.toMutableList().also { it[1] = "32801" },
            saved.toMutableList().also { it[6] = 1024 },
        )) assertNull(HomeManagerUpdates.restorePending(invalid))
    }

    @Test fun permissionTransitionSavesReleaseBeforeInstallerCanLaunchSettings() = runBlocking {
        var pending: LatestVersionInfo? = null
        runHomeManagerUpdate(release, { pending = it }) {
            assertEquals(release, pending)
            assertEquals(release.copy(changelog = ""), HomeManagerUpdates.restorePending(HomeManagerUpdates.savePending(pending)))
            true
        }
        assertEquals(release, pending)
    }

    @Test fun installerOpenedClearsPendingRelease() = runBlocking {
        var pending: LatestVersionInfo? = null
        runHomeManagerUpdate(release, { pending = it }) {
            assertEquals(release, pending)
            false
        }
        assertNull(pending)
    }

    @Test fun anExplicitFailureClearsPendingAndPropagates() = runBlocking {
        var pending: LatestVersionInfo? = null
        try {
            runHomeManagerUpdate(release, { pending = it }) { throw IllegalStateException("rejected") }
            fail("Failure must propagate")
        } catch (_: IllegalStateException) {
            assertNull(pending)
        }
    }

    @Test fun cancellationClearsPendingAndPropagates() = runBlocking {
        var pending: LatestVersionInfo? = null
        try {
            runHomeManagerUpdate(release, { pending = it }) { throw CancellationException("cancelled") }
            fail("Cancellation must propagate")
        } catch (_: CancellationException) {
            assertNull(pending)
        }
    }

    @Test fun aRestoredPermissionRetryUsesAllOriginalVerificationFields() = runBlocking {
        val restored = HomeManagerUpdates.restorePending(HomeManagerUpdates.savePending(release))!!
        var pending: LatestVersionInfo? = restored
        runHomeManagerUpdate(restored, { pending = it }) { offered ->
            assertEquals(release.copy(changelog = ""), offered)
            assertEquals(release.assetSha256, offered.assetSha256)
            assertEquals(release.assetSize, offered.assetSize)
            false
        }
        assertNull(pending)
    }

    @Test fun refreshAndFailedFetchPreserveAValidOffer() {
        assertEquals(release, HomeManagerUpdates.offer(release, 32800, true, endpoint))
        assertEquals(release, HomeManagerUpdates.offer(release, 32800, true, endpoint, LatestVersionInfo()))
    }

    @Test fun disablingUpdatesOrInstallingTheOfferedVersionClearsIt() {
        assertEquals(LatestVersionInfo(), HomeManagerUpdates.offer(release, 32800, false, endpoint))
        assertEquals(LatestVersionInfo(), HomeManagerUpdates.offer(release, 32801, true, endpoint))
        assertEquals(LatestVersionInfo(), HomeManagerUpdates.offer(release, 32802, true, endpoint))
    }

    @Test fun invalidOrOlderFetchedOffersDoNotReplaceTheValidOffer() {
        assertEquals(release, HomeManagerUpdates.offer(
            release, 32800, true, endpoint, release.copy(downloadUrl = "https://example.com/update.apk"),
        ))
        val older = release.copy(
            versionCode = 32800, assetName = "XKCNSU_0.2.1_32800.apk",
            downloadUrl = release.downloadUrl.replace("32801.apk", "32800.apk"),
        )
        assertEquals(release, HomeManagerUpdates.offer(release, 32799, true, endpoint, older))
    }

    @Test fun aNewerVerifiedOfferReplacesThePreviousOne() {
        val newer = release.copy(
            versionCode = 32802, assetName = "XKCNSU_0.2.1_32802.apk",
            downloadUrl = release.downloadUrl.replace("32801.apk", "32802.apk"),
        )
        assertEquals(newer, HomeManagerUpdates.offer(release, 32800, true, endpoint, newer))
    }

    @Test fun successfulSameVersionMetadataRefreshReplacesThePreviousOffer() {
        val refreshed = release.copy(changelog = "Updated notes", assetSha256 = "b".repeat(64))
        assertEquals(refreshed, HomeManagerUpdates.offer(release, 32800, true, endpoint, refreshed))
    }

    @Test fun previouslyInvalidMetadataIsNotRetainedThroughAnOfflineRefresh() {
        assertEquals(LatestVersionInfo(), HomeManagerUpdates.offer(
            release.copy(assetSha256 = "invalid"), 32800, true, endpoint,
        ))
    }

    @Test fun multiMegabyteChangelogIsNotWrittenIntoSavedState() {
        val saved = HomeManagerUpdates.savePending(release.copy(changelog = "x".repeat(2 * 1024 * 1024)))
        assertEquals("", saved[3])
        assertTrue(saved.filterIsInstance<String>().sumOf { it.length } < 8192)
        assertEquals(release.copy(changelog = ""), HomeManagerUpdates.restorePending(saved))
    }

    @Test fun oversizedVerificationStringsCannotOverflowSavedState() {
        for (invalid in listOf(
            release.copy(downloadUrl = "x".repeat(4097)),
            release.copy(version = "x".repeat(129)),
            release.copy(assetName = "x".repeat(256)),
            release.copy(assetSha256 = "a".repeat(65)),
        )) {
            assertTrue(HomeManagerUpdates.savePending(invalid).isEmpty())
        }
        val malformed = HomeManagerUpdates.savePending(release).toMutableList()
        malformed[5] = "x".repeat(256)
        assertNull(HomeManagerUpdates.restorePending(malformed))
    }
}
