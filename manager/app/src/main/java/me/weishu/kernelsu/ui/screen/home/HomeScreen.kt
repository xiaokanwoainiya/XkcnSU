package me.weishu.kernelsu.ui.screen.home

import android.content.Intent
import android.widget.Toast
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.saveable.listSaver
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalResources
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.Dp
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import me.weishu.kernelsu.R
import me.weishu.kernelsu.magica.MagicaService
import me.weishu.kernelsu.ksuApp
import me.weishu.kernelsu.ui.LocalUiMode
import me.weishu.kernelsu.ui.UiMode
import me.weishu.kernelsu.ui.component.dialog.rememberLoadingDialog
import me.weishu.kernelsu.ui.navigation3.Navigator
import me.weishu.kernelsu.ui.navigation3.Route
import me.weishu.kernelsu.ui.util.CoreManagerUpdater
import me.weishu.kernelsu.ui.util.module.LatestVersionInfo
import me.weishu.kernelsu.ui.viewmodel.HomeViewModel
import me.weishu.kernelsu.ui.viewmodel.KpmViewModel
import kotlin.time.Duration.Companion.milliseconds

@Composable
fun HomePager(
    navigator: Navigator,
    bottomInnerPadding: Dp,
    isCurrentPage: Boolean = true,
) {
    val viewModel = viewModel<HomeViewModel>()
    val integrationViewModel = viewModel<KpmViewModel>(viewModelStoreOwner = ksuApp)
    val uiState by viewModel.uiState.collectAsStateWithLifecycle()
    val integrationState by integrationViewModel.uiState.collectAsStateWithLifecycle()
    val homeState = uiState.copy(
        susfsInfo = if (uiState.isLateLoadMode) {
            SusfsHomeInfo()
        } else {
            uiState.susfsInfo.merge(SusfsHomeSnapshot.from(integrationState))
        },
    )
    val uriHandler = LocalUriHandler.current
    val context = LocalContext.current
    val resources = LocalResources.current
    val loadingDialog = rememberLoadingDialog()
    val scope = rememberCoroutineScope()
    val latestIsCurrentPage by rememberUpdatedState(isCurrentPage)
    val initialResumeHandled = rememberSaveable { mutableStateOf(false) }
    var managerUpdateRunning by remember { mutableStateOf(false) }
    var pendingManagerUpdate by rememberSaveable(stateSaver = PendingManagerUpdateSaver) {
        mutableStateOf<LatestVersionInfo?>(null)
    }

    val installManagerUpdate: (LatestVersionInfo) -> Unit = { release ->
        if (!managerUpdateRunning) {
            managerUpdateRunning = true
            scope.launch {
                loadingDialog.showLoading()
                try {
                    runHomeManagerUpdate(release, { pendingManagerUpdate = it }) {
                        CoreManagerUpdater.downloadAndInstall(context, it) ==
                            CoreManagerUpdater.Outcome.INSTALL_PERMISSION_REQUIRED
                    }
                } catch (error: Exception) {
                    if (error is CancellationException && error !is TimeoutCancellationException) throw error
                    Toast.makeText(
                        context,
                        resources.getString(
                            R.string.download_failed_content,
                            release.assetName.ifBlank { resources.getString(R.string.app_name) },
                        ),
                        Toast.LENGTH_LONG,
                    ).show()
                } finally {
                    loadingDialog.hide()
                    managerUpdateRunning = false
                }
            }
        }
    }
    val latestInstallManagerUpdate by rememberUpdatedState(installManagerUpdate)

    LaunchedEffect(viewModel, integrationViewModel) {
        viewModel.observeSusfs(integrationViewModel.uiState)
    }
    LaunchedEffect(isCurrentPage) {
        if (isCurrentPage) {
            viewModel.refresh()
        }
    }

    LifecycleResumeEffect(Unit) {
        if (initialResumeHandled.value && latestIsCurrentPage) {
            viewModel.refresh()
        }
        pendingManagerUpdate?.takeIf { context.packageManager.canRequestPackageInstalls() }
            ?.let(latestInstallManagerUpdate)
        initialResumeHandled.value = true
        onPauseOrDispose { }
    }

    val actions = HomeActions(
        onInstallClick = { navigator.push(Route.Install) },
        onOpenUrl = uriHandler::openUri,
        onUpdateClick = installManagerUpdate,
        onJailbreakClick = {
            loadingDialog.showLoading()
            context.startService(Intent(context, MagicaService::class.java))
            // Manager will be force-stopped and restarted by late-load on success.
            // If that doesn't happen within timeout, jailbreak likely failed.
            scope.launch(Dispatchers.IO) {
                delay(30_000.milliseconds)
                withContext(Dispatchers.Main) {
                    loadingDialog.hide()
                    Toast.makeText(context, R.string.jailbreak_timeout, Toast.LENGTH_LONG).show()
                }
            }
        },
    )

    when (LocalUiMode.current) {
        UiMode.Miuix -> HomePagerMiuix(
            state = homeState,
            actions = actions,
            bottomInnerPadding = bottomInnerPadding,
        )

        UiMode.Material -> HomePagerMaterial(
            state = homeState,
            actions = actions,
            bottomInnerPadding = bottomInnerPadding,
        )
    }
}

private val PendingManagerUpdateSaver = listSaver<LatestVersionInfo?, Any>(
    save = { HomeManagerUpdates.savePending(it) },
    restore = HomeManagerUpdates::restorePending,
)

@Composable
internal fun SusfsHomeInfo.summary(): String {
    return summary(
        supportedLabel = stringResource(R.string.home_susfs_supported),
        unavailableLabel = stringResource(R.string.kpm_unavailable),
        unknownLabel = stringResource(R.string.selinux_status_unknown),
    )
}
