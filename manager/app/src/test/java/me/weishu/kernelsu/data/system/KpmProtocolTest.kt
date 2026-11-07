package me.weishu.kernelsu.data.system

import me.weishu.kernelsu.data.model.MainTab
import me.weishu.kernelsu.ui.screen.kpm.KpmUiState
import me.weishu.kernelsu.ui.screen.kpm.SusfsState
import org.junit.Assert.*
import org.junit.Test

class KpmProtocolTest {
    @Test fun confirmedNavigationPreferenceSurvivesUntilARealStatusArrives() {
        val cached = KpmUiState(kpmTabEnabled = true, navigationReady = true)
        assertTrue(cached.navigationReady)
        assertTrue(cached.kpmTabEnabled)
        assertEquals(5, MainTab.visible(cached.kpmTabEnabled).size)
        assertTrue(cached.merge(KpmReport(error = BackendException("query", "offline"))).kpmTabEnabled)
        assertFalse(cached.merge(KpmReport(KpmStatus(true, false, true, true), emptyList())).kpmTabEnabled)
        assertTrue(KpmUiState().merge(KpmReport(KpmStatus(true, true, false, true), emptyList())).kpmTabEnabled)
        assertFalse(KpmUiState().navigationReady)
        assertTrue(KpmUiState().merge(KpmReport(KpmStatus(true, true, true, false), emptyList())).navigationReady)
    }

    @Test fun importActionDoesNotDisappearDuringMutationsOrFailedRefreshes() {
        val ready = KpmUiState().merge(KpmReport(KpmStatus(true, true, true, false), emptyList()))
        assertTrue(ready.showImport)
        assertTrue(ready.copy(busy = true).showImport)
        assertFalse(ready.copy(busy = true).canImport)
        assertTrue(ready.merge(KpmReport(error = BackendException("query", "failed"))).showImport)
        assertFalse(ready.copy(unavailable = "No root").showImport)
    }

    @Test fun refreshPreservesPendingImportAndTracksActualLoadedState() {
        val module = KpmModule("a", "A", "1", "", "", "", "hash", false, false, false, hasControl = true)
        val state = KpmUiState(pendingImport = module, pendingImportIsNew = true)
        val confirmed = module.copy(loaded = true)
        val failure = BackendException("autoload_failed", "failed")
        val refreshed = state.merge(KpmReport(
            status = KpmStatus(true, true, true, false),
            modules = listOf(confirmed),
            error = failure,
        ))
        assertEquals(confirmed, refreshed.pendingImport)
        assertTrue(refreshed.pendingImportIsNew)
        assertSame(failure, refreshed.error)
        assertEquals(confirmed, refreshed.modules.single())
        assertEquals(confirmed, refreshed.merge(KpmReport(detail = module)).pendingImport)
    }

    private fun output(data: String, code: Int = 0, stderr: String = "") =
        CommandOutput(code, """{"ok":true,"data":$data,"error":null}""", stderr)

    @Test fun statusAcceptsNullVersionAndExtraFields() {
        val status = KpmProtocol.decode<KpmStatus>(output(
            """{"supported":true,"configured_enabled":false,"active":false,"reboot_required":false,"version":null,"extra":1}"""
        ))
        assertEquals(IntegrationState.OFF, integrationState(status))
        assertNull(status.version)
        assertTrue(status.integrationVerified)
        assertFalse(status.canUnload)
    }

    @Test fun runtimeStatusUsesCurrentPreflightEligibilityAndUpgradeWithoutClaimingVerification() {
        val verified = KpmStatus(
            supported = true, configuredEnabled = true, active = true, rebootRequired = false,
            version = "old", canConfigure = true, canEnable = true, canDisable = true,
            upgradeAvailable = true, canUnload = false,
        )
        val full = KpmUiState(canPatchBoot = true).merge(KpmReport(verified, emptyList()))
        val runtime = KpmProtocol.decode<KpmStatus>(output(
            """{"supported":true,"configured_enabled":true,"active":true,"reboot_required":false,"version":"new","can_configure":false,"can_enable":false,"can_disable":false,"upgrade_available":false,"integration_verified":false,"can_unload":true}"""
        ))
        val refreshed = full.merge(KpmReport(runtime, emptyList(), statusQueried = true))
        assertFalse(refreshed.status!!.integrationVerified)
        assertEquals("new", refreshed.status.version)
        assertTrue(refreshed.status.canUnload)
        assertEquals(false, refreshed.status.canConfigure)
        assertEquals(false, refreshed.status.canEnable)
        assertEquals(false, refreshed.status.canDisable)
        assertFalse(refreshed.status.upgradeAvailable)
        assertFalse(refreshed.hasFreshIntegrationStatus)
        assertFalse(refreshed.canSetIntegration(false))
        assertFalse(refreshed.canUpgradeIntegration)
        assertTrue(refreshed.canLoad)
        assertFalse(refreshed.merge(KpmReport(runtime, emptyList(), statusQueried = true)).canUpgradeIntegration)
    }

    @Test fun freshRuntimeStatusCanRequestUpgradePreflightWithoutAnyFullImageQuery() {
        val runtime = KpmStatus(
            supported = true, configuredEnabled = true, active = true, rebootRequired = false,
            canEnable = true, canDisable = true, upgradeAvailable = true, integrationVerified = false,
        )
        val report = KpmReport(runtime, emptyList(), statusQueried = true)
        val state = KpmUiState(canPatchBoot = true).merge(report)
        assertTrue(state.hasFreshStatus)
        assertFalse(state.hasFreshIntegrationStatus)
        assertFalse(state.status!!.integrationVerified)
        assertTrue(state.showUpgradeIntegration)
        assertTrue(state.canUpgradeIntegration)
        assertTrue(state.canSetIntegration(false))
        assertFalse(report.verifiesIntegrationUpgrade)
        assertFalse(state.copy(busy = true).canUpgradeIntegration)
        assertFalse(state.copy(safeMode = true).canUpgradeIntegration)
        assertFalse(state.copy(canPatchBoot = false).canUpgradeIntegration)
        assertFalse(state.copy(unavailable = "No root").canUpgradeIntegration)
        assertFalse(state.copy(status = runtime.copy(stateKnown = false)).canUpgradeIntegration)
        assertFalse(state.copy(status = runtime.copy(canEnable = false)).canUpgradeIntegration)
        assertFalse(state.copy(status = runtime.copy(canEnable = null)).canUpgradeIntegration)
    }

    @Test fun runtimeDetailsCannotRecoverFailedStatusOrModuleSnapshotAccess() {
        val runtime = KpmStatus(
            supported = true, configuredEnabled = true, active = true, rebootRequired = false,
            canEnable = true, upgradeAvailable = true, integrationVerified = false,
        )
        val report = KpmReport(runtime, emptyList(), statusQueried = true)
        val ready = KpmUiState(canPatchBoot = true).merge(report)
        assertTrue(ready.canUpgradeIntegration)
        for (failedReport in listOf(
            KpmReport(error = BackendException("status", "failed"), statusQueried = true),
            KpmReport(status = runtime, error = BackendException("list", "failed"), statusQueried = true),
            KpmReport(error = BackendException("info", "failed")),
        )) {
            val failed = ready.merge(failedReport)
            assertFalse(failed.canUpgradeIntegration)
            assertFalse(failed.merge(KpmReport()).canUpgradeIntegration)
            assertFalse(failed.merge(KpmReport(modules = emptyList())).canUpgradeIntegration)
            assertTrue(failed.merge(report).canUpgradeIntegration)
        }
    }

    @Test fun currentRuntimeUpgradeDetectionSupersedesPreviousFullImageEligibility() {
        val verified = KpmStatus(
            supported = true, configuredEnabled = true, active = true, rebootRequired = false,
            canEnable = false, canDisable = false, upgradeAvailable = false,
        )
        val state = KpmUiState(canPatchBoot = true).merge(KpmReport(verified, emptyList()))
        val runtime = verified.copy(
            canEnable = true, canDisable = true, upgradeAvailable = true, integrationVerified = false,
        )
        val ready = state.merge(KpmReport(runtime, emptyList(), statusQueried = true))
        assertTrue(ready.showUpgradeIntegration)
        assertTrue(ready.canUpgradeIntegration)
        assertFalse(ready.status!!.integrationVerified)
        val current = ready.merge(KpmReport(runtime.copy(upgradeAvailable = false), emptyList(), statusQueried = true))
        assertFalse(current.showUpgradeIntegration)
        assertFalse(current.canUpgradeIntegration)
    }

    @Test fun missingRuntimeRequestFlagsRequireAMatchingFreshVerification() {
        val verified = KpmStatus(
            supported = true, configuredEnabled = true, active = true, rebootRequired = false,
            canEnable = true, canDisable = true, upgradeAvailable = true,
        )
        val ready = KpmUiState(canPatchBoot = true).merge(KpmReport(verified, emptyList()))
        val runtime = verified.copy(
            canEnable = null, canDisable = null, canConfigure = true, integrationVerified = false,
        )
        val current = ready.merge(KpmReport(runtime, emptyList(), statusQueried = true))
        assertNull(current.status!!.canEnable)
        assertNull(current.status.canDisable)
        assertTrue(current.canUpgradeIntegration)
        assertTrue(current.canChangeIntegration)
        assertFalse(current.copy(integrationVerification = null).canUpgradeIntegration)
        assertFalse(current.copy(integrationVerification = null).canChangeIntegration)
    }

    @Test fun firstEnableAndDisableFollowExplicitRuntimeRequestFlags() {
        for (enabled in listOf(false, true)) {
            for (canEnable in listOf(false, true)) {
                for (canDisable in listOf(false, true)) {
                    val runtime = KpmStatus(
                        supported = true, configuredEnabled = enabled, active = enabled, rebootRequired = false,
                        canEnable = canEnable, canDisable = canDisable,
                        canConfigure = !(if (enabled) canDisable else canEnable),
                        integrationVerified = false,
                    )
                    val ready = KpmUiState(canPatchBoot = true).merge(KpmReport(runtime, emptyList()))
                    assertFalse(ready.hasFreshIntegrationStatus)
                    assertEquals(if (enabled) canDisable else canEnable, ready.canChangeIntegration)
                    assertFalse(ready.copy(status = runtime.copy(stateKnown = false)).canChangeIntegration)
                    assertFalse(ready.copy(safeMode = true).canSetIntegration(true))
                    assertFalse(ready.showUpgradeIntegration)
                }
            }
        }
    }

    @Test fun runtimeStatusCannotCreateOrRestoreFullIntegrationVerification() {
        val runtime = KpmStatus(
            supported = true, configuredEnabled = true, active = true, rebootRequired = false,
            canConfigure = false, canEnable = false, canDisable = false, integrationVerified = false,
        )
        val fresh = KpmUiState(canPatchBoot = true).merge(KpmReport(runtime, emptyList(), statusQueried = true))
        assertTrue(fresh.canLoad)
        assertFalse(fresh.hasFreshIntegrationStatus)
        assertFalse(fresh.canChangeIntegration)
        val full = fresh.merge(KpmReport(runtime.copy(integrationVerified = true, canDisable = true), emptyList()))
        assertTrue(full.canSetIntegration(false))
        val failed = full.merge(KpmReport(error = BackendException("status", "failed"), statusQueried = true))
        val recoveredRuntime = failed.merge(KpmReport(runtime, emptyList(), statusQueried = true))
        assertTrue(recoveredRuntime.canLoad)
        assertFalse(recoveredRuntime.canChangeIntegration)
        assertFalse(recoveredRuntime.hasFreshIntegrationStatus)
    }

    @Test fun changedOrUnknownIntegrationStateInvalidatesCachedFullControls() {
        val verified = KpmStatus(
            supported = true, configuredEnabled = true, active = true, rebootRequired = false,
            canConfigure = true, canEnable = true, canDisable = true,
        )
        val full = KpmUiState(canPatchBoot = true).merge(KpmReport(verified, emptyList()))
        val runtime = verified.copy(
            integrationVerified = false, canConfigure = false, canEnable = false, canDisable = false,
        )
        for (changed in listOf(
            runtime.copy(configuredEnabled = false, rebootRequired = true),
            runtime.copy(transitionFailed = true),
            runtime.copy(stateKnown = false),
        )) {
            val refreshed = full.merge(KpmReport(changed, emptyList(), statusQueried = true))
            assertFalse(refreshed.hasFreshIntegrationStatus)
            assertFalse(refreshed.canChangeIntegration)
            assertFalse(refreshed.canLoad)
        }
    }

    @Test fun loadedRemovalRequiresTrueUnloadCapabilityButUnloadedFilesCanBeRemoved() {
        val module = KpmModule("a", "A", "1", "", "", "", "hash", false, true, false, hasControl = true)
        val legacy = KpmUiState().merge(KpmReport(
            KpmStatus(true, true, true, false, canUnload = false), listOf(module),
        ))
        assertTrue(legacy.canLoad)
        assertFalse(legacy.canUnload(module))
        assertFalse(legacy.canRemove(module))
        assertTrue(legacy.canRemove(module.copy(loaded = false)))
        val current = legacy.merge(KpmReport(legacy.status!!.copy(canUnload = true), listOf(module)))
        assertTrue(current.canUnload(module))
        assertTrue(current.canRemove(module))
        assertFalse(current.canRemove(module.copy(pendingUnload = true)))
        assertFalse(current.copy(busy = true).canRemove(module))
    }

    @Test fun stateDistinguishesReadOffPendingActiveUnavailableAndFailure() {
        assertEquals(IntegrationState.READING, integrationState(null))
        val off = KpmStatus(true, false, false, false)
        assertEquals(IntegrationState.OFF, integrationState(off))
        assertEquals(IntegrationState.PENDING_REBOOT, integrationState(off.copy(configuredEnabled = true, rebootRequired = true)))
        assertEquals(IntegrationState.PENDING_REBOOT, integrationState(off.copy(active = true, rebootRequired = true)))
        assertEquals(IntegrationState.ACTIVE, integrationState(off.copy(active = true, configuredEnabled = true)))
        assertEquals(IntegrationState.UNAVAILABLE, integrationState(off.copy(supported = false)))
        assertEquals(IntegrationState.UNAVAILABLE, integrationState(null, unavailable = "No root"))
        assertEquals(IntegrationState.ERROR, integrationState(off, error = BackendException("io", "failed")))
    }

    @Test fun failedBootIsAnErrorEvenWhenConfiguredAndInactive() {
        val status = KpmProtocol.decode<KpmStatus>(output(
            """{"supported":true,"configured_enabled":true,"active":false,"reboot_required":false,"transition_failed":true,"reason":"KPM engine did not start after reboot"}"""
        ))
        assertEquals(IntegrationState.ERROR, integrationState(status))
        assertEquals("KPM engine did not start after reboot", status.reason)
    }

    @Test fun disabledEngineStillActiveAfterRebootIsAnError() {
        val status = KpmProtocol.decode<KpmStatus>(output(
            """{"supported":true,"configured_enabled":false,"active":true,"reboot_required":false,"transition_failed":true,"reason":"KPM engine is still running after reboot"}"""
        ))
        val state = KpmUiState(canPatchBoot = true).merge(KpmReport(status, emptyList()))
        assertEquals(IntegrationState.ERROR, state.integration)
        assertEquals("KPM engine is still running after reboot", state.status!!.reason)
        assertTrue(state.status.active)
        assertFalse(state.requiresReboot)
        assertFalse(state.canReboot)
        assertFalse(state.canLoad)
    }

    @Test fun unconfiguredUnmanagedEngineRemainsUnavailable() {
        val status = KpmProtocol.decode<KpmStatus>(output(
            """{"supported":false,"configured_enabled":false,"active":true,"reboot_required":false,"transition_failed":false,"provider":"unmanaged"}"""
        ))
        val state = KpmUiState(canPatchBoot = true).merge(KpmReport(status, emptyList()))
        assertEquals(IntegrationState.UNAVAILABLE, state.integration)
        assertFalse(state.status!!.transitionFailed)
        assertFalse(state.requiresReboot)
        assertFalse(state.canReboot)
        assertFalse(state.canChangeIntegration)
        assertFalse(state.canLoad)
    }

    @Test fun verifiedEngineUpgradeCanBeConfiguredBeforeRuntimeIsCompatible() {
        val status = KpmProtocol.decode<KpmStatus>(output(
            """{"supported":false,"can_configure":true,"configured_enabled":false,"active":true,"reboot_required":false,"provider":"unmanaged","reason":"Verified boot image can be upgraded"}"""
        ))
        val state = KpmUiState(canPatchBoot = true).merge(KpmReport(status, emptyList()))
        assertTrue(state.canSetIntegration(true))
        assertFalse(state.canLoad)
        assertFalse(state.copy(busy = true).canSetIntegration(true))
        assertFalse(state.copy(safeMode = true).canSetIntegration(true))
        assertFalse(state.copy(status = status.copy(canConfigure = false)).canSetIntegration(true))
        assertFalse(state.copy(status = status.copy(canConfigure = null)).canSetIntegration(true))
    }

    @Test fun verifiedDisableRemainsAvailableWhenEnableToolsFail() {
        val status = KpmProtocol.decode<KpmStatus>(output(
            """{"supported":false,"can_configure":false,"can_enable":false,"can_disable":true,"configured_enabled":true,"active":true,"reboot_required":false,"upgrade_available":true}"""
        ))
        val state = KpmUiState(canPatchBoot = true).merge(KpmReport(
            status = status,
            error = BackendException("list", "Runtime query failed"),
        ))
        assertEquals(false, status.canEnable)
        assertEquals(true, status.canDisable)
        assertTrue(state.canChangeIntegration)
        assertTrue(state.canSetIntegration(false))
        assertTrue(state.copy(safeMode = true).canSetIntegration(false))
        assertFalse(state.canSetIntegration(true))
        assertFalse(state.canUpgradeIntegration)
        assertFalse(state.copy(busy = true).canSetIntegration(false))
        assertFalse(state.copy(unavailable = "No root").canSetIntegration(false))
        assertFalse(state.copy(canPatchBoot = false).canSetIntegration(false))
        assertFalse(state.copy(hasFreshIntegrationStatus = false).canSetIntegration(false))
        assertFalse(state.copy(status = status.copy(stateKnown = false)).canSetIntegration(false))
    }

    @Test fun switchUsesTheCapabilityForItsNextDirection() {
        val off = KpmStatus(
            supported = true, configuredEnabled = false, active = false, rebootRequired = false,
            canConfigure = true, canEnable = false, canDisable = true,
        )
        val state = KpmUiState(canPatchBoot = true).merge(KpmReport(status = off))
        assertFalse(state.canChangeIntegration)
        assertFalse(state.canSetIntegration(true))
        assertFalse(state.canSetIntegration(false))
        val enabled = state.copy(status = off.copy(
            configuredEnabled = true, active = true, upgradeAvailable = true,
            canEnable = true, canDisable = false,
        ))
        assertFalse(enabled.canChangeIntegration)
        assertFalse(enabled.canSetIntegration(false))
        assertTrue(enabled.canSetIntegration(true))
        assertTrue(enabled.canUpgradeIntegration)
        assertFalse(enabled.copy(safeMode = true).canSetIntegration(true))
    }

    @Test fun absentDirectionalFlagsRetainTheLegacyProtocolFallback() {
        for (enabled in listOf(false, true)) {
            for (legacy in listOf<Boolean?>(null, false, true)) {
                val status = KpmStatus(
                    supported = true, configuredEnabled = enabled, active = enabled,
                    rebootRequired = false, canConfigure = legacy,
                )
                val state = KpmUiState(canPatchBoot = true).merge(KpmReport(status = status))
                assertNull(status.canEnable)
                assertNull(status.canDisable)
                assertEquals(legacy != false, state.canChangeIntegration)
                assertEquals(legacy != false, state.canSetIntegration(!enabled))
                assertFalse(state.copy(status = status.copy(
                    canConfigure = true, canEnable = false, canDisable = false,
                )).canSetIntegration(!enabled))
            }
        }
    }

    @Test fun unverifiedUnknownEngineCannotUseEitherDirectionalCapability() {
        val status = KpmProtocol.decode<KpmStatus>(output(
            """{"supported":false,"can_configure":false,"can_enable":false,"can_disable":false,"state_known":false,"configured_enabled":false,"active":true,"reboot_required":false}"""
        ))
        val state = KpmUiState(canPatchBoot = true).merge(KpmReport(status = status))
        assertFalse(state.canChangeIntegration)
        assertFalse(state.canSetIntegration(true))
        assertFalse(state.canSetIntegration(false))
        val forgedCapabilities = state.copy(status = status.copy(
            canConfigure = true, canEnable = true, canDisable = true,
            configuredEnabled = true, upgradeAvailable = true,
        ))
        assertFalse(forgedCapabilities.canChangeIntegration)
        assertFalse(forgedCapabilities.canSetIntegration(true))
        assertFalse(forgedCapabilities.canSetIntegration(false))
    }

    @Test fun unknownImageStateDoesNotEraseTheConfirmedNavigationPreference() {
        val unknown = KpmStatus(
            supported = true, configuredEnabled = false, active = true, rebootRequired = false,
            canConfigure = false, stateKnown = false,
        )
        val cached = KpmUiState(kpmTabEnabled = true, navigationReady = true, canPatchBoot = true)
        val state = cached.merge(KpmReport(status = unknown, modules = emptyList()))
        assertTrue(state.kpmTabEnabled)
        assertTrue(state.navigationReady)
        assertFalse(state.canChangeIntegration)
        assertFalse(KpmUiState().merge(KpmReport(status = unknown)).navigationReady)
    }

    @Test fun enabledLegacyIntegrationHasAnExplicitUpgradeAction() {
        val status = KpmProtocol.decode<KpmStatus>(output(
            """{"supported":false,"can_configure":true,"configured_enabled":true,"active":true,"reboot_required":false,"upgrade_available":true}"""
        ))
        val state = KpmUiState(canPatchBoot = true).merge(KpmReport(status, emptyList()))
        assertTrue(state.showUpgradeIntegration)
        assertTrue(state.canUpgradeIntegration)
        assertTrue(state.canSetIntegration(true))
        assertTrue(state.canSetIntegration(false))
        assertFalse(state.canLoad)
        assertFalse(state.copy(safeMode = true).canSetIntegration(true))
        assertTrue(state.copy(safeMode = true).canSetIntegration(false))
        assertFalse(state.copy(busy = true).canSetIntegration(true))
        val current = state.copy(status = status.copy(upgradeAvailable = false))
        assertFalse(current.showUpgradeIntegration)
        assertFalse(current.canUpgradeIntegration)
        assertFalse(current.canSetIntegration(true))
    }

    @Test fun updateRowOnlyReplacesAnEnabledOutdatedIntegrationAndDoesNotFlickerWhileBusy() {
        for (enabled in listOf(false, true)) {
            for (outdated in listOf(false, true)) {
                val state = KpmUiState(canPatchBoot = true).merge(KpmReport(
                    KpmStatus(
                        supported = true, configuredEnabled = enabled, active = enabled,
                        rebootRequired = false, canEnable = true, canDisable = true,
                        upgradeAvailable = outdated,
                    ), emptyList(),
                ))
                assertEquals(enabled && outdated, state.showUpgradeIntegration)
                assertEquals(state.showUpgradeIntegration, state.copy(busy = true).showUpgradeIntegration)
                assertEquals(enabled && outdated, state.canUpgradeIntegration)
                assertFalse(state.copy(busy = true).canUpgradeIntegration)
                assertFalse(state.copy(safeMode = true).canUpgradeIntegration)
            }
        }
    }

    @Test fun rebootAfterUpgradeRequiresAConfirmedFullStatusWithTheNewEngineStaged() {
        val staged = KpmStatus(
            supported = true, configuredEnabled = true, active = true,
            rebootRequired = true, upgradeAvailable = false,
        )
        assertTrue(KpmReport(status = staged).verifiesIntegrationUpgrade)
        for (unverified in listOf(
            staged.copy(integrationVerified = false),
            staged.copy(stateKnown = false),
            staged.copy(configuredEnabled = false),
            staged.copy(transitionFailed = true),
            staged.copy(upgradeAvailable = true),
            staged.copy(rebootRequired = false),
        )) {
            assertFalse(KpmReport(status = unverified).verifiesIntegrationUpgrade)
        }
        assertFalse(KpmReport().verifiesIntegrationUpgrade)
        assertFalse(KpmReport(status = staged, error = BackendException("verify", "failed")).verifiesIntegrationUpgrade)
    }

    @Test fun exitFailureCannotBeHiddenBySuccessfulEnvelope() {
        val error = assertThrows(BackendException::class.java) {
            KpmProtocol.unwrap(output("[]", 9, "permission denied"))
        }
        assertEquals(9, error.exitCode)
        assertTrue(error.display.contains("permission denied"))
    }

    @Test fun structuredErrorPreservesCodeMessageAndExit() {
        val error = assertThrows(BackendException::class.java) {
            KpmProtocol.unwrap(CommandOutput(1,
                """{"ok":false,"data":null,"error":{"code":"busy","message":"in use"}}""", "trace"))
        }
        assertEquals("busy", error.code)
        assertEquals("in use", error.message)
        assertEquals(1, error.exitCode)
    }

    @Test fun rejectsMalformedIncompleteAndStringBooleanEnvelopes() {
        listOf("", "log\n{}", """{"ok":true,"data":[]}""", """{"ok":"true","data":[],"error":null}""").forEach {
            assertThrows(BackendException::class.java) { KpmProtocol.unwrap(CommandOutput(0, it, "")) }
        }
        assertThrows(BackendException::class.java) {
            KpmProtocol.decode<KpmStatus>(output("""{"active":false}"""))
        }
    }

    @Test fun failedRefreshPreservesReadableStateButDisablesMutationsUntilFullRecovery() {
        val module = KpmModule("a", "A", "1", "author", "", "", "hash", false, true, false, hasControl = false)
        val snapshot = KpmReport(KpmStatus(true, true, true, false, canUnload = true), listOf(module))
        val active = KpmUiState(canPatchBoot = true).merge(snapshot, module.id)
        assertTrue(active.canImport)
        assertTrue(active.canLoad)
        assertTrue(active.canUnload(module))
        assertTrue(active.canRemove(module.copy(loaded = false)))
        assertTrue(active.canChangeIntegration)
        assertTrue(active.canSetIntegration(false))

        val failed = active.merge(KpmReport(error = BackendException("query", "failed"), statusQueried = true), module.id)
        assertEquals(active.modules, failed.modules)
        assertEquals(active.status, failed.status)
        assertEquals(active.detail, failed.detail)
        assertEquals(IntegrationState.ERROR, failed.integration)
        assertFalse(failed.hasFreshStatus)
        assertFalse(failed.canImport)
        assertFalse(failed.canLoad)
        assertFalse(failed.canUnload(module))
        assertFalse(failed.canRemove(module.copy(loaded = false)))
        assertFalse(failed.canChangeIntegration)
        assertFalse(failed.canSetIntegration(false))

        val detailOnly = failed.merge(KpmReport(detail = module))
        assertEquals(module, detailOnly.detail)
        assertSame(failed.error, detailOnly.error)
        assertFalse(detailOnly.hasFreshStatus)
        assertFalse(detailOnly.canImport)
        assertFalse(detailOnly.canLoad)
        assertFalse(detailOnly.canChangeIntegration)

        val recovered = detailOnly.merge(snapshot, module.id)
        assertNull(recovered.error)
        assertTrue(recovered.hasFreshStatus)
        assertTrue(recovered.canImport)
        assertTrue(recovered.canLoad)
        assertTrue(recovered.canUnload(module))
        assertTrue(recovered.canChangeIntegration)
        assertTrue(recovered.canSetIntegration(false))
    }

    @Test fun detailAndPartialSnapshotsCannotEstablishFreshness() {
        val module = KpmModule("a", "A", "1", "author", "", "", "hash", false, false, false, hasControl = false)
        val status = KpmStatus(true, true, true, false)
        val detailOnly = KpmUiState().merge(KpmReport(detail = module))
        assertFalse(detailOnly.canImport)
        assertFalse(detailOnly.canLoad)
        val statusOnly = detailOnly.merge(KpmReport(status = status))
        assertFalse(statusOnly.hasFreshStatus)
        assertFalse(statusOnly.canImport)

        val full = KpmUiState(canPatchBoot = true).merge(KpmReport(status, listOf(module)))
        val failedList = full.merge(KpmReport(status = status, error = BackendException("list", "failed")))
        assertEquals(listOf(module), failedList.modules)
        assertFalse(failedList.canLoad)
        assertTrue(failedList.canChangeIntegration)
        val failedStatus = full.merge(KpmReport(
            modules = listOf(module), error = BackendException("status", "failed"), statusQueried = true,
        ))
        assertEquals(status, failedStatus.status)
        assertFalse(failedStatus.canImport)
        assertFalse(failedStatus.canLoad)
    }

    @Test fun rebootAndIntegrationConfirmationsFollowFreshStatusAndCurrentConfiguration() {
        val pending = KpmUiState(canPatchBoot = true).merge(
            KpmReport(KpmStatus(true, true, false, true), emptyList())
        )
        assertTrue(pending.canChangeIntegration)
        assertTrue(pending.canSetIntegration(false))
        assertFalse(pending.canSetIntegration(true))
        assertTrue(pending.canReboot)
        val failed = pending.merge(KpmReport(error = BackendException("query", "failed"), statusQueried = true))
        assertTrue(failed.requiresReboot)
        assertFalse(failed.canReboot)
        assertFalse(failed.canSetIntegration(false))
        assertFalse(pending.copy(busy = true).canReboot)
        assertFalse(pending.copy(unavailable = "No root").canChangeIntegration)
        assertFalse(pending.copy(canPatchBoot = false).canSetIntegration(false))

        val active = pending.merge(KpmReport(KpmStatus(true, true, true, false), emptyList()))
        assertFalse(active.canReboot)
        val off = active.merge(KpmReport(KpmStatus(true, false, false, false), emptyList()))
        assertFalse(off.canSetIntegration(false))
        assertTrue(off.canSetIntegration(true))
        assertFalse(off.copy(safeMode = true).canSetIntegration(true))
        assertTrue(active.copy(safeMode = true).canSetIntegration(false))
    }

    @Test fun susfsQueryErrorDisablesMutationsWithoutDiscardingCachedStatus() {
        val active = KpmUiState(susfs = SusfsState(
            supported = true, loaded = true, builtin = false, autoload = false
        ))
        assertTrue(active.canMutateSusfs)
        val failed = active.copy(susfs = active.susfs.queryFailed(BackendException("query", "failed")))
        assertEquals(active.susfs.loaded, failed.susfs.loaded)
        assertEquals(active.susfs.autoload, failed.susfs.autoload)
        assertFalse(failed.canMutateSusfs)
        assertFalse(active.copy(busy = true).canMutateSusfs)
        assertFalse(active.copy(unavailable = "No root").canMutateSusfs)
        assertFalse(KpmUiState().canMutateSusfs)
        assertTrue(failed.copy(susfs = active.susfs).canMutateSusfs)
    }

    @Test fun integrationCanBeEnabledBeforeAnyModuleListIsAvailable() {
        val state = KpmUiState(canPatchBoot = true).merge(KpmReport(
            status = KpmStatus(true, false, false, false),
            error = BackendException("list", "Registry cannot be read"),
        ))
        assertTrue(state.hasFreshIntegrationStatus)
        assertFalse(state.hasFreshStatus)
        assertTrue(state.canSetIntegration(true))
        assertFalse(state.canImport)
        assertFalse(state.copy(busy = true).canSetIntegration(true))
        assertFalse(state.copy(safeMode = true).canSetIntegration(true))
        assertFalse(state.copy(unavailable = "No root").canSetIntegration(true))
        assertFalse(state.copy(status = state.status!!.copy(supported = false)).canSetIntegration(true))
    }

    @Test fun failedIntegrationCanBeRetriedAfterTheActualStateIsRead() {
        val active = KpmUiState(canPatchBoot = true).merge(KpmReport(
            status = KpmStatus(true, true, true, false),
            modules = emptyList(),
            error = BackendException("operation_failed", "Temporary I/O failure"),
        ))
        assertTrue(active.hasFreshIntegrationStatus)
        assertTrue(active.canSetIntegration(false))
        assertFalse(active.canLoad)
        val failedQuery = active.merge(KpmReport(error = BackendException("query", "offline"), statusQueried = true))
        assertFalse(failedQuery.hasFreshIntegrationStatus)
        assertFalse(failedQuery.canSetIntegration(false))
        assertFalse(failedQuery.merge(KpmReport(detail = null)).canSetIntegration(false))
        val refreshed = failedQuery.merge(KpmReport(status = active.status))
        assertTrue(refreshed.canSetIntegration(false))
    }

    @Test fun failedModuleDetailsDoNotInvalidateIntegrationStatus() {
        val active = KpmUiState(canPatchBoot = true).merge(KpmReport(
            status = KpmStatus(true, true, true, false), modules = emptyList(),
        ))
        val failed = active.merge(KpmReport(error = BackendException("not_found", "Module is missing")))
        assertTrue(failed.hasFreshIntegrationStatus)
        assertTrue(failed.canSetIntegration(false))
        assertFalse(failed.canLoad)
    }

    @Test fun losingAccessRequiresEachIntegrationToBeReadAgain() {
        val active = KpmUiState(canPatchBoot = true).merge(KpmReport(
            status = KpmStatus(true, true, true, false), modules = emptyList(),
        )).copy(susfs = SusfsState(
            supported = true, loaded = true, builtin = false, autoload = true,
        ))
        val denied = active.withAccess("No root", canPatchBoot = false, safeMode = false)
        assertTrue(denied.susfsIntegrationEnabled)
        assertTrue(denied.kpmTabEnabled)
        val accessRestored = denied.withAccess(null, canPatchBoot = true, safeMode = false)
        assertFalse(accessRestored.canChangeIntegration)
        assertFalse(accessRestored.canChangeSusfsIntegration)
        val kpmRefreshed = accessRestored.merge(KpmReport(status = active.status, modules = emptyList()))
        assertTrue(kpmRefreshed.canChangeIntegration)
        assertFalse(kpmRefreshed.canChangeSusfsIntegration)
        val susfsRefreshed = accessRestored.copy(susfs = active.susfs)
        assertTrue(susfsRefreshed.canChangeSusfsIntegration)
        assertFalse(susfsRefreshed.canChangeIntegration)
    }

    @Test fun cannotLoadWhileDisablingOrAfterFailedBoot() {
        val active = KpmUiState().merge(KpmReport(KpmStatus(true, true, true, false), emptyList()))
        assertTrue(active.canLoad)
        assertFalse(active.copy(status = active.status!!.copy(configuredEnabled = false)).canLoad)
        assertFalse(active.copy(status = active.status.copy(transitionFailed = true)).canLoad)
        assertFalse(active.copy(busy = true).canLoad)
        assertFalse(active.copy(safeMode = true).canLoad)
    }

    @Test fun integrationStartsOffAndRechecksBothRebootDirections() {
        val initial = KpmUiState(canPatchBoot = true)
        assertFalse(initial.status?.configuredEnabled ?: false)
        assertFalse(initial.canChangeIntegration)
        assertFalse(initial.canReboot)

        val off = initial.merge(KpmReport(KpmStatus(true, false, false, false), emptyList()))
        val enabling = off.merge(KpmReport(KpmStatus(true, true, false, true), emptyList()))
        assertEquals(IntegrationState.PENDING_REBOOT, enabling.integration)
        assertTrue(enabling.canReboot)
        assertFalse(enabling.canLoad)
        assertTrue(enabling.status!!.configuredEnabled)

        val active = enabling.merge(KpmReport(KpmStatus(true, true, true, false), emptyList()))
        assertEquals(IntegrationState.ACTIVE, active.integration)
        assertTrue(active.canLoad)
        assertFalse(active.canReboot)

        val disabling = active.merge(KpmReport(KpmStatus(true, false, true, true), emptyList()))
        assertEquals(IntegrationState.PENDING_REBOOT, disabling.integration)
        assertTrue(disabling.canReboot)
        assertFalse(disabling.canLoad)
        assertFalse(disabling.status!!.configuredEnabled)

        val disabled = disabling.merge(KpmReport(KpmStatus(true, false, false, false), emptyList()))
        assertEquals(IntegrationState.OFF, disabled.integration)
        assertFalse(disabled.canReboot)
        assertFalse(disabled.canLoad)
    }

    @Test fun failedBootAllowsDisablingWithoutOfferingAnotherRebootAsPending() {
        val failed = KpmUiState(canPatchBoot = true).merge(KpmReport(
            KpmStatus(true, true, false, false, transitionFailed = true),
            emptyList(),
        ))
        assertEquals(IntegrationState.ERROR, failed.integration)
        assertFalse(failed.requiresReboot)
        assertFalse(failed.canReboot)
        assertFalse(failed.canLoad)
        assertTrue(failed.canSetIntegration(false))
    }

    @Test fun mismatchedStateWithoutBackendRebootRequirementIsAnError() {
        for (enabled in listOf(true, false)) {
            val status = KpmStatus(true, enabled, !enabled, false)
            val state = KpmUiState(canPatchBoot = true).merge(KpmReport(status, emptyList()))
            assertEquals(IntegrationState.ERROR, state.integration)
            assertFalse(state.requiresReboot)
            assertFalse(state.canReboot)
            assertFalse(state.canLoad)
            assertEquals(enabled, state.status!!.configuredEnabled)
        }
    }

    @Test fun pendingUnloadCannotEnableAutoloadButCanStillDisableIt() {
        val module = KpmModule("a", "A", "1", "author", "", "", "hash", false, true, false, hasControl = false)
        val state = KpmUiState().merge(KpmReport(KpmStatus(true, true, true, false), listOf(module)))
        assertTrue(state.canChangeAutoload(module))
        assertFalse(state.canChangeAutoload(module.copy(pendingUnload = true)))
        assertTrue(state.canChangeAutoload(module.copy(pendingUnload = true, autoload = true)))
        assertFalse(state.copy(safeMode = true).canChangeAutoload(module))
        assertTrue(state.copy(safeMode = true).canChangeAutoload(module.copy(autoload = true)))
        assertFalse(state.copy(busy = true).canChangeAutoload(module))
        assertFalse(state.canChangeAutoload(module.copy(sha256 = "")))
        assertFalse(state.canChangeAutoload(null))
        assertFalse(state.merge(KpmReport(error = BackendException("query", "failed"))).canChangeAutoload(module))
    }

    @Test fun navigationRestoresStableNamesAndMigratesFourPageIndices() {
        assertEquals(listOf(MainTab.HOME, MainTab.KPM, MainTab.SUPERUSER, MainTab.MODULE, MainTab.SETTINGS), MainTab.entries.toList())
        assertEquals(2, MainTab.SUPERUSER.ordinal)
        assertEquals(MainTab.KPM, MainTab.restore("KPM"))
        assertEquals(MainTab.SUPERUSER, MainTab.restore(null, 1))
        assertEquals(MainTab.MODULE, MainTab.restore(null, 2))
        assertEquals(MainTab.SETTINGS, MainTab.restore(null, 3))
        assertEquals(MainTab.HOME, MainTab.restore("invalid", 200))
    }

    @Test fun navigationOnlyIncludesKpmWhenIntegrationIsConfigured() {
        assertEquals(
            listOf(MainTab.HOME, MainTab.SUPERUSER, MainTab.MODULE, MainTab.SETTINGS),
            MainTab.visible(false),
        )
        assertEquals(MainTab.entries.toList(), MainTab.visible(true))
        val pending = KpmStatus(true, true, false, true)
        assertEquals(MainTab.KPM, MainTab.visible(pending.configuredEnabled)[1])
        val disabling = KpmStatus(true, false, true, true)
        assertFalse(MainTab.visible(disabling.configuredEnabled).contains(MainTab.KPM))
    }

    @Test fun navigationKeepsTheSelectedDestinationWhenKpmChanges() {
        for (tab in MainTab.visible(false)) {
            assertEquals(tab, MainTab.destination(tab.name, MainTab.visible(true)))
            assertEquals(tab, MainTab.destination(tab.name, MainTab.visible(false)))
        }
        assertEquals(MainTab.HOME, MainTab.destination(MainTab.KPM.name, MainTab.visible(false)))
        assertEquals(MainTab.KPM, MainTab.destination(MainTab.KPM.name, MainTab.visible(true)))
    }

    @Test fun coreUpdateHasNoImplicitUpstreamFallback() {
        assertNull(CoreUpdatePolicy.url(""))
        assertNull(CoreUpdatePolicy.url("http://example.com/release"))
        assertNull(CoreUpdatePolicy.url("https://user:pass@example.com/release"))
        assertEquals("https://example.com/release", CoreUpdatePolicy.url("https://example.com/release"))
        assertEquals(30001, CoreUpdatePolicy.versionCode("XKCNSU_v1.0_30001-release.apk"))
        assertEquals(30001, CoreUpdatePolicy.versionCode("XKCNSU_PR_v1.0_30001.apk"))
        assertNull(CoreUpdatePolicy.versionCode("KernelSU_v1.0_30001-release.apk"))
    }
}
