package me.weishu.kernelsu.ui.viewmodel

import android.content.Context
import android.net.Uri
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import me.weishu.kernelsu.Natives
import me.weishu.kernelsu.R
import me.weishu.kernelsu.data.system.BackendException
import me.weishu.kernelsu.data.system.KpmImport
import me.weishu.kernelsu.data.system.KpmReport
import me.weishu.kernelsu.data.system.SystemBackends
import me.weishu.kernelsu.data.system.SusfsProtocol
import me.weishu.kernelsu.data.system.backendError
import me.weishu.kernelsu.ksuApp
import me.weishu.kernelsu.ui.screen.home.HomeStartupState
import me.weishu.kernelsu.ui.screen.home.SusfsHomeCache
import me.weishu.kernelsu.ui.screen.home.SusfsHomeSnapshot
import me.weishu.kernelsu.ui.screen.kpm.KpmUiState
import me.weishu.kernelsu.ui.util.getRootShell

class KpmViewModel : ViewModel() {
    private val integrationPreferences = ksuApp.getSharedPreferences("system_integration", Context.MODE_PRIVATE)
    private val _uiState = MutableStateFlow(
        if (Natives.isLateLoadMode) jailbreakState() else KpmUiState(
            kpmTabEnabled = integrationPreferences.getBoolean("kpm_enabled", false),
            navigationReady = integrationPreferences.contains("kpm_enabled"),
        )
    )
    val uiState = _uiState.asStateFlow()
    private val _homeStartupState = MutableStateFlow(
        HomeStartupState(
            cachedSusfsEnabled = when {
                Natives.isLateLoadMode -> false
                integrationPreferences.contains("home_susfs_enabled") ->
                    integrationPreferences.getBoolean("home_susfs_enabled", false)
                else -> null
            },
        ),
    )
    val homeStartupState = _homeStartupState.asStateFlow()
    private val operations = Mutex()
    private var integrationVerificationJob: Job? = null
    private var detailId: String? = null

    private fun jailbreakState() = KpmUiState(
        unavailable = ksuApp.getString(R.string.jailbreak_integrations_disabled),
        navigationReady = true,
    )

    fun prewarmIntegration() {
        if (integrationVerificationJob?.isActive == true) return
        integrationVerificationJob = viewModelScope.launch {
            val report = withContext(Dispatchers.IO) {
                val eligible = runCatching {
                    !Natives.isLateLoadMode && Natives.isManager &&
                        Natives.kernelUAPIVersion == Natives.managerUAPIVersion
                }.getOrDefault(false)
                if (eligible) SystemBackends.kpm.verifyIntegration(SystemBackends.integrationVerifier) else null
            }
            report?.let { checked ->
                if (checked.status != null && SystemBackends.kpm.isCurrentVerification(checked) &&
                    _uiState.value.status?.sameIntegrationState(checked.status) == false
                ) {
                    // Full verification may recover a missing integration record.
                    refresh().join()
                }
                _uiState.update {
                    if (!Natives.isLateLoadMode && !it.busy && SystemBackends.kpm.isCurrentVerification(checked)) {
                        it.mergeIntegrationVerification(checked)
                    } else it
                }
            }
        }
    }

    fun refresh(includeSusfs: Boolean = false, verifyIntegration: Boolean = false): Job {
        if (verifyIntegration) prewarmIntegration()
        return operation(mutating = false, completesSusfsStartup = includeSusfs) {
            if (includeSusfs) refreshSusfs()
            if (_uiState.value.pendingImport == null) {
                applyReport(SystemBackends.kpm.refresh())
            }
        }
    }

    fun showDetail(id: String) = operation {
        if (detailId != id) _uiState.update { it.copy(detail = null, output = null) }
        detailId = id
        applyReport(SystemBackends.kpm.info(id))
    }

    fun importDocument(uri: Uri) = operation {
        if (_uiState.value.pendingImport != null) throw BackendException("busy", "A KPM import is pending")
        val result = KpmImport.importDocument(ksuApp, uri, SystemBackends.kpm)
        applyReport(result.report)
        if (result.report.error == null && result.report.detail != null) {
            _uiState.update {
                it.copy(pendingImport = result.report.detail, pendingImportIsNew = result.isNew, output = null)
            }
        }
    }

    fun selectModuleForLoad(id: String) {
        _uiState.update { state ->
            val module = state.modules.find { it.id == id }
            if (state.busy || state.pendingImport != null || !state.canLoad ||
                module == null || module.loaded || module.pendingUnload
            ) state else state.copy(pendingImport = module, pendingImportIsNew = false, output = null)
        }
    }

    fun dismissOutput() {
        _uiState.update { it.copy(output = null) }
    }

    fun dismissError() {
        _uiState.update { it.copy(error = null) }
    }

    fun cancelImport() = operation {
        val pending = _uiState.value.pendingImport ?: return@operation
        if (_uiState.value.pendingImportIsNew) {
            val current = SystemBackends.kpm.list()
            applyReport(current)
            if (current.error != null) return@operation
            val module = current.modules?.find { it.id == pending.id }
            if (module != null && !module.loaded && !module.pendingUnload) {
                val removed = SystemBackends.kpm.mutate(listOf("kpm", "remove", pending.id))
                applyReport(removed)
                if (removed.error != null) return@operation
                if (removed.modules?.any { it.id == pending.id } != false) {
                    throw BackendException("unconfirmed_remove", "KPM removal was not confirmed")
                }
            }
        }
        _uiState.update { it.copy(pendingImport = null, pendingImportIsNew = false) }
    }

    fun loadImported(temporary: Boolean) = operation {
        val state = _uiState.value
        val pending = state.pendingImport ?: return@operation
        if (state.safeMode) throw BackendException("safe_mode", "Loading is disabled in safe mode")
        val report = SystemBackends.kpm.loadImported(pending.id, pending.args, temporary)
        applyReport(report)
        _uiState.update {
            if (report.error == null) {
                it.copy(pendingImport = null, pendingImportIsNew = false)
            } else {
                it.copy(pendingImport = report.modules?.find { module -> module.id == pending.id } ?: it.pendingImport)
            }
        }
    }

    fun integrate(enabled: Boolean) = mutate(listOf("integration", if (enabled) "enable" else "disable"))

    fun upgradeIntegration() = operation {
        if (!_uiState.value.copy(busy = false).canUpgradeIntegration) {
            throw BackendException("upgrade_unavailable", "A KPM integration upgrade is not available")
        }
        val report = SystemBackends.kpm.mutate(listOf("kpm", "integration", "enable"))
        applyReport(report)
        report.error?.let { return@operation }
        if (!report.verifiesIntegrationUpgrade) {
            throw BackendException("unconfirmed_upgrade", "The KPM engine upgrade was not verified")
        }
        rebootDevice()
    }

    fun fullReboot() = operation { rebootDevice() }

    private fun rebootDevice() {
        val shell = getRootShell(true)
        if (!shell.isRoot) throw BackendException("root_unavailable", "Root access is required")
        val stdout = mutableListOf<String>()
        val stderr = mutableListOf<String>()
        val result = shell.newJob().add("/system/bin/svc power reboot || /system/bin/reboot")
            .to(stdout, stderr).exec()
        if (result.code != 0) {
            throw BackendException("reboot_failed", stdout.joinToString("\n"), result.code, stderr.joinToString("\n"))
        }
    }

    fun moduleAction(id: String, action: String, args: String = "") {
        val command = when (action) {
            "load", "control" -> listOf(action, id, "--args", args)
            "unload", "remove" -> listOf(action, id)
            "autoload_enable" -> listOf("autoload", id, "enable")
            "autoload_disable" -> listOf("autoload", id, "disable")
            else -> return
        }
        mutate(command)
    }

    private fun mutate(arguments: List<String>) = operation {
        if (_uiState.value.safeMode && (
            arguments.firstOrNull() == "load" ||
                (arguments.firstOrNull() == "integration" && arguments.getOrNull(1) == "enable") ||
                (arguments.firstOrNull() == "autoload" && arguments.getOrNull(2) == "enable")
            )) {
            throw BackendException("safe_mode", "Loading and enabling integration are disabled in safe mode")
        }
        applyReport(SystemBackends.kpm.mutate(listOf("kpm") + arguments))
    }

    fun susfsAction(action: String, kmi: String = "") = operation {
        var failure: BackendException? = null
        try {
            if (_uiState.value.safeMode && action in listOf("load", "enable")) {
                throw BackendException("safe_mode", "SUSFS loading is disabled in safe mode")
            }
            val arguments = when (action) {
                "load" -> listOf("susfs", "load") + kmiArguments(kmi)
                "unload" -> listOf("susfs", "unload")
                "enable" -> {
                    val selected = kmi.ifBlank {
                        _uiState.value.susfs.kmi.ifBlank {
                            SusfsProtocol.checkCommand(SystemBackends.executor.execute(listOf("boot-info", "current-kmi"))).stdout.trim()
                        }
                    }
                    if (!selected.matches(Regex("[A-Za-z0-9._-]+"))) {
                        throw BackendException("kmi_required", "Select a valid KMI")
                    }
                    SusfsProtocol.enableAutoload(SystemBackends.executor, selected)
                    null
                }
                "disable" -> {
                    SusfsProtocol.disableIntegration(SystemBackends.executor)
                    null
                }
                else -> throw BackendException("invalid_action", "Unknown SUSFS action")
            }
            arguments?.let { SusfsProtocol.checkCommand(SystemBackends.executor.execute(it)) }
        } catch (e: Exception) {
            failure = e.backendError()
        }
        refreshSusfs()
        if (failure != null) _uiState.update { it.copy(susfs = it.susfs.copy(error = failure)) }
    }

    private fun kmiArguments(kmi: String): List<String> {
        if (kmi.isBlank()) return emptyList()
        if (!kmi.matches(Regex("[A-Za-z0-9._-]+"))) throw BackendException("invalid_kmi", "Invalid KMI")
        return listOf("--kmi", kmi)
    }

    private suspend fun refreshSusfs() {
        var state = _uiState.value.susfs
        var confirmedEnabled: Boolean? = null
        try {
            val status = SusfsProtocol.decodeStatus(SystemBackends.executor.execute(listOf("susfs", "status")))
            state = state.merge(status)
            val confirmed = _uiState.value.copy(susfs = state)
            val enabled = confirmed.susfsIntegrationEnabled
            confirmedEnabled = enabled
            val cached = SusfsHomeCache(
                available = integrationPreferences.getBoolean(SusfsHomeCache.AVAILABLE_KEY, false),
                version = integrationPreferences.getString(SusfsHomeCache.VERSION_KEY, null),
                variant = integrationPreferences.getString(SusfsHomeCache.VARIANT_KEY, null),
                builtin = if (integrationPreferences.contains(SusfsHomeCache.BUILTIN_KEY)) {
                    integrationPreferences.getBoolean(SusfsHomeCache.BUILTIN_KEY, false)
                } else null,
            ).restore(integrationPreferences.getBoolean("home_susfs_enabled", false))
            val cache = SusfsHomeCache.from(cached.merge(SusfsHomeSnapshot.from(confirmed)))
            integrationPreferences.edit().apply {
                putBoolean("home_susfs_enabled", enabled)
                putBoolean(SusfsHomeCache.AVAILABLE_KEY, cache.available)
                putString(SusfsHomeCache.VERSION_KEY, cache.version)
                putString(SusfsHomeCache.VARIANT_KEY, cache.variant)
                cache.builtin?.let {
                    putBoolean(SusfsHomeCache.BUILTIN_KEY, it)
                } ?: remove(SusfsHomeCache.BUILTIN_KEY)
                apply()
            }
        } catch (e: Exception) {
            state = state.queryFailed(e.backendError())
        }
        _uiState.update { it.copy(susfs = state) }
        _homeStartupState.update { it.completeSusfsQuery(confirmedEnabled) }
    }

    private fun applyReport(report: KpmReport) {
        report.status?.takeIf { it.stateKnown }?.let {
            integrationPreferences.edit().putBoolean("kpm_enabled", it.configuredEnabled).apply()
        }
        _uiState.update { it.merge(report, detailId) }
    }

    private fun operation(
        mutating: Boolean = true,
        completesSusfsStartup: Boolean = false,
        block: suspend () -> Unit,
    ) = viewModelScope.launch {
        operations.withLock {
            if (Natives.isLateLoadMode) {
                _uiState.value = jailbreakState()
                _homeStartupState.update { it.completeSusfsQuery(false) }
                return@withLock
            }
            if (mutating) _uiState.update { it.copy(busy = true) }
            try {
                val (unavailable, canPatchBoot, safeMode) = withContext(Dispatchers.IO) {
                    val reason = when {
                        !runCatching { Natives.isManager }.getOrDefault(false) -> "XKCNSU manager authentication is unavailable"
                        !runCatching { Natives.kernelUAPIVersion == Natives.managerUAPIVersion }.getOrDefault(false) ->
                            "Matching kernel UAPI is required"
                        !runCatching { getRootShell(true).isRoot }.getOrDefault(false) -> "Root access is required"
                        else -> null
                    }
                    Triple(
                        reason,
                        reason == null,
                        reason == null && runCatching { Natives.isSafeMode }.getOrDefault(true),
                    )
                }
                _uiState.update {
                    it.withAccess(unavailable, canPatchBoot, safeMode)
                }
                if (unavailable == null) withContext(Dispatchers.IO) { block() }
            } catch (e: Exception) {
                _uiState.update { it.copy(error = e.backendError()) }
            } finally {
                if (completesSusfsStartup) {
                    _homeStartupState.update { it.completeSusfsQuery(null) }
                }
                if (mutating) _uiState.update { it.copy(busy = false) }
            }
        }
    }
}
