package me.weishu.kernelsu.ui.util

import android.net.Uri
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.withContext
import me.weishu.kernelsu.BuildConfig
import me.weishu.kernelsu.data.registry.ExpectedFile
import me.weishu.kernelsu.data.system.CoreUpdatePolicy
import me.weishu.kernelsu.ksuApp
import me.weishu.kernelsu.ui.util.module.LatestVersionInfo
import okhttp3.Request

/**
 * @author weishu
 * @date 2023/6/22.
 */
suspend fun download(
    url: String,
    fileName: String,
    onDownloaded: (Uri) -> Unit = {},
    onDownloading: () -> Unit = {},
    onProgress: (Int) -> Unit = {},
    expected: ExpectedFile? = null,
    onFailed: (DownloadManager.DownloadState) -> Unit = {},
) {
    onDownloading()

    val downloadId = DownloadManager.enqueue(
        context = ksuApp,
        url = url,
        fileName = fileName,
        onCompleted = onDownloaded,
        expected = expected,
    )

    val finalState = DownloadManager.downloads
        .onEach { map -> map[downloadId]?.let { onProgress(it.progress) } }
        .first { map ->
            val status = map[downloadId]?.status
            status == DownloadManager.Status.COMPLETED ||
                status == DownloadManager.Status.FAILED
        }[downloadId]
    if (finalState?.status == DownloadManager.Status.FAILED) {
        withContext(Dispatchers.Main) { onFailed(finalState) }
    }
}

internal suspend fun isDownloadAvailable(uri: Uri): Boolean = withContext(Dispatchers.IO) {
    runCatching {
        ksuApp.contentResolver.openFileDescriptor(uri, "r").use { it != null }
    }.getOrDefault(false)
}

fun checkNewVersion(): LatestVersionInfo {
    if (!BuildConfig.CORE_UPDATE_ENABLED || BuildConfig.IS_PR_BUILD) return LatestVersionInfo()
    val url = CoreUpdatePolicy.url(BuildConfig.XKCNSU_UPDATE_URL) ?: return LatestVersionInfo()
    if (!isNetworkAvailable(ksuApp)) return LatestVersionInfo()
    return runCatching {
        val request = Request.Builder()
            .url(url)
            .header("Accept", "application/vnd.github+json")
            .header("X-GitHub-Api-Version", "2022-11-28")
            .build()
        ksuApp.okhttpClient.newCall(request).execute()
            .use { response ->
                if (response.code != 200) {
                    return@use LatestVersionInfo()
                }
                val limit = 2L * 1024 * 1024
                if (response.body.contentLength() > limit) return@use LatestVersionInfo()
                val source = response.body.source()
                val body = java.io.ByteArrayOutputStream()
                val buffer = ByteArray(8192)
                while (true) {
                    val count = source.read(buffer)
                    if (count == -1) break
                    if (body.size().toLong() + count > limit) {
                        return@use LatestVersionInfo()
                    }
                    body.write(buffer, 0, count)
                }
                CoreUpdatePolicy.release(
                    statusCode = response.code,
                    body = body.toString(Charsets.UTF_8.name()),
                    currentVersionCode = BuildConfig.VERSION_CODE,
                    endpoint = url,
                )
            }
    }.getOrDefault(LatestVersionInfo())
}
