package me.weishu.kernelsu.ui.viewmodel

import android.content.Context
import android.os.Build
import android.system.Os
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import me.weishu.kernelsu.BuildConfig
import me.weishu.kernelsu.Natives
import me.weishu.kernelsu.data.repository.SettingsRepository
import me.weishu.kernelsu.data.repository.SettingsRepositoryImpl
import me.weishu.kernelsu.getKernelVersion
import me.weishu.kernelsu.ksuApp
import me.weishu.kernelsu.ui.screen.home.HomeUiState
import me.weishu.kernelsu.ui.screen.home.HomeManagerUpdates
import me.weishu.kernelsu.ui.screen.home.SystemInfo
import me.weishu.kernelsu.ui.screen.home.SusfsHomeCache
import me.weishu.kernelsu.ui.screen.home.getManagerVersion
import me.weishu.kernelsu.ui.screen.home.susfsHomeSnapshots
import me.weishu.kernelsu.ui.screen.kpm.KpmUiState
import me.weishu.kernelsu.ui.util.checkNewVersion
import me.weishu.kernelsu.ui.util.getSELinuxStatusRaw
import me.weishu.kernelsu.ui.util.module.LatestVersionInfo
import me.weishu.kernelsu.ui.util.resolveDeviceName
import me.weishu.kernelsu.ui.util.rootAvailable

class HomeViewModel(
    private val settingsRepo: SettingsRepository = SettingsRepositoryImpl()
) : ViewModel() {

    private val integrationPreferences = ksuApp.getSharedPreferences("system_integration", Context.MODE_PRIVATE)
    private val susfsRefreshRequests = MutableStateFlow(0)
    private var susfsObserver: Job? = null
    private var refreshJob: Job? = null
    private val _uiState = MutableStateFlow(
        buildState().copy(
            susfsInfo = SusfsHomeCache(
                available = integrationPreferences.getBoolean(SusfsHomeCache.AVAILABLE_KEY, false),
                version = integrationPreferences.getString(SusfsHomeCache.VERSION_KEY, null),
                variant = integrationPreferences.getString(SusfsHomeCache.VARIANT_KEY, null),
                builtin = if (integrationPreferences.contains(SusfsHomeCache.BUILTIN_KEY)) {
                    integrationPreferences.getBoolean(SusfsHomeCache.BUILTIN_KEY, false)
                } else null,
            ).restore(integrationPreferences.getBoolean("home_susfs_enabled", false)),
        ),
    )
    val uiState: StateFlow<HomeUiState> = _uiState.asStateFlow()

    fun refresh() {
        refreshSusfsInfo()
        refreshJob?.cancel()
        refreshJob = viewModelScope.launch {
            val baseState = withContext(Dispatchers.IO) { buildState() }
            _uiState.update {
                baseState.copy(
                    susfsInfo = it.susfsInfo,
                    latestVersionInfo = HomeManagerUpdates.offer(
                        it.latestVersionInfo, baseState.currentManagerVersionCode,
                        updatesEnabled(), BuildConfig.XKCNSU_UPDATE_URL,
                    ),
                )
            }
            if (updatesEnabled()) {
                val latestVersionInfo = withContext(Dispatchers.IO) { checkNewVersion() }
                _uiState.update {
                    it.copy(
                        latestVersionInfo = HomeManagerUpdates.offer(
                            it.latestVersionInfo, it.currentManagerVersionCode,
                            updatesEnabled(), BuildConfig.XKCNSU_UPDATE_URL, latestVersionInfo,
                        ),
                    )
                }
            }
        }
    }

    private fun updatesEnabled() =
        BuildConfig.CORE_UPDATE_ENABLED && !BuildConfig.IS_PR_BUILD && settingsRepo.checkUpdate

    fun refreshSusfsInfo() {
        susfsRefreshRequests.update { it + 1 }
    }

    fun observeSusfs(integrationState: StateFlow<KpmUiState>) {
        if (susfsObserver?.isActive == true) return
        susfsObserver = viewModelScope.launch {
            susfsHomeSnapshots(integrationState, susfsRefreshRequests).collectLatest { snapshot ->
                _uiState.update { it.copy(susfsInfo = it.susfsInfo.merge(snapshot)) }
                if (snapshot.confirmed) {
                    val cache = SusfsHomeCache.from(_uiState.value.susfsInfo)
                    integrationPreferences.edit().apply {
                        putBoolean(SusfsHomeCache.AVAILABLE_KEY, cache.available)
                        putString(SusfsHomeCache.VERSION_KEY, cache.version)
                        putString(SusfsHomeCache.VARIANT_KEY, cache.variant)
                        cache.builtin?.let {
                            putBoolean(SusfsHomeCache.BUILTIN_KEY, it)
                        } ?: remove(SusfsHomeCache.BUILTIN_KEY)
                        apply()
                    }
                }
            }
        }
    }

    private fun buildState(): HomeUiState {
        val kernelVersion = getKernelVersion()
        val isManager = Natives.isManager
        val ksuVersion = if (isManager) Natives.version else null
        val kernelUAPIVersion = if (isManager) Natives.kernelUAPIVersion else null
        val managerUAPIVersion = Natives.managerUAPIVersion
        val lkmMode = ksuVersion?.let { if (kernelVersion.isGKI()) Natives.isLkmMode else null }
        val isRootAvailable = rootAvailable()
        val managerVersion = getManagerVersion(ksuApp)

        return HomeUiState(
            kernelVersion = kernelVersion,
            ksuVersion = ksuVersion,
            lkmMode = lkmMode,
            isLkmBundled = lkmMode == true && Natives.isLkmBundled,
            isManager = isManager,
            isManagerPrBuild = BuildConfig.IS_PR_BUILD,
            isKernelPrBuild = Natives.isPrBuild,
            requiresNewKernel = isManager && Natives.managerUAPIVersion > Natives.kernelUAPIVersion,
            requiresNewManager = isManager && Natives.managerUAPIVersion < Natives.kernelUAPIVersion,
            kernelUAPIVersion = kernelUAPIVersion,
            managerUAPIVersion = managerUAPIVersion,
            isRootAvailable = isRootAvailable,
            isSafeMode = Natives.isSafeMode,
            isLateLoadMode = Natives.isLateLoadMode,
            checkUpdateEnabled = settingsRepo.checkUpdate,
            latestVersionInfo = LatestVersionInfo(),
            currentManagerVersionCode = managerVersion.versionCode,
            bundledKernelVersionCode = BuildConfig.BUNDLED_KERNEL_VERSION_CODE,
            systemInfo = SystemInfo(
                kernelVersion = Os.uname().release,
                managerVersion = "${managerVersion.versionName} (${ksuVersion ?: BuildConfig.BUNDLED_KERNEL_VERSION_CODE}-${managerUAPIVersion})",
                deviceModel = resolveDeviceName(),
                fingerprint = Build.FINGERPRINT,
                selinuxStatus = getSELinuxStatusRaw(),
                seccompStatus = runCatching {
                    Os.prctl(21 /* PR_GET_SECCOMP */, 0, 0, 0, 0)
                }.getOrDefault(-1),
            ),
        )
    }
}
