package me.weishu.kernelsu.ui.util

import android.content.ClipData
import android.content.Context
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import androidx.core.content.FileProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import me.weishu.kernelsu.BuildConfig
import me.weishu.kernelsu.data.registry.RegistryUrls
import me.weishu.kernelsu.data.system.CoreApkIdentity
import me.weishu.kernelsu.data.system.CoreManagerUpdatePolicy
import me.weishu.kernelsu.data.system.CoreUpdatePolicy
import me.weishu.kernelsu.ksuApp
import me.weishu.kernelsu.ui.util.module.LatestVersionInfo
import okhttp3.Call
import okhttp3.Callback
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import java.io.File
import java.io.IOException
import java.security.MessageDigest
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

object CoreManagerUpdater {
    enum class Outcome { INSTALLER_OPENED, INSTALL_PERMISSION_REQUIRED }

    private const val APK_TYPE = "application/vnd.android.package-archive"
    private val operation = Mutex()
    private val mainHandler = Handler(Looper.getMainLooper())

    /** Retry with the same release after granting installation permission; the saved APK is revalidated. */
    suspend fun downloadAndInstall(
        context: Context,
        release: LatestVersionInfo,
        onProgress: (Int) -> Unit = {},
    ): Outcome = operation.withLock {
        withTimeout(180_000L) {
            if (!BuildConfig.CORE_UPDATE_ENABLED || BuildConfig.IS_PR_BUILD) {
                throw IOException("Manager updates are disabled for this build")
            }
            val app = context.applicationContext
            val prepared = withContext(Dispatchers.IO) {
                prepare(app, release, onProgress)
            }
            val installFile = if (ManagerHiding.isHidden) ManagerHiding.repackageUpdate(prepared.first) else prepared.first
            withContext(Dispatchers.Main) {
                onProgress(100)
                if (!app.packageManager.canRequestPackageInstalls()) {
                    app.startActivity(Intent(
                        Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                        Uri.parse("package:${app.packageName}"),
                    ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                    Outcome.INSTALL_PERMISSION_REQUIRED
                } else {
                    withContext(Dispatchers.IO) {
                        validateSaved(app, prepared.first, release, prepared.second)
                    }
                    val uri = FileProvider.getUriForFile(
                        app, "${app.packageName}.fileprovider", installFile,
                    )
                    app.startActivity(Intent(Intent.ACTION_VIEW).apply {
                        setDataAndType(uri, APK_TYPE)
                        clipData = ClipData.newRawUri("XKCNSU update", uri)
                        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
                    })
                    Outcome.INSTALLER_OPENED
                }
            }
        }
    }

    @Suppress("DEPRECATION")
    private fun installed(context: Context): PackageInfo =
        context.packageManager.getPackageInfo(context.packageName, PackageManager.GET_SIGNING_CERTIFICATES)

    private fun identity(info: PackageInfo): CoreApkIdentity {
        val signers = info.signingInfo?.apkContentsSigners
            ?.map { certificate ->
                MessageDigest.getInstance("SHA-256").digest(certificate.toByteArray())
                    .joinToString("") { "%02x".format(it) }
            }?.toSet().orEmpty()
        val application = info.applicationInfo ?: throw IOException("Cannot read APK application metadata")
        return CoreApkIdentity(
            packageName = info.packageName,
            versionCode = info.longVersionCode,
            versionName = info.versionName,
            signerSha256 = signers,
            debuggable = application.flags and ApplicationInfo.FLAG_DEBUGGABLE != 0,
            testOnly = application.flags and ApplicationInfo.FLAG_TEST_ONLY != 0,
            split = !info.splitNames.isNullOrEmpty(),
        )
    }

    private suspend fun prepare(
        context: Context,
        release: LatestVersionInfo,
        onProgress: (Int) -> Unit,
    ): Pair<File, String> {
        val installed = installed(context)
        if (!CoreUpdatePolicy.isDownloadable(
                release, installed.longVersionCode, BuildConfig.XKCNSU_UPDATE_URL,
            ) || release.assetSize > CoreManagerUpdatePolicy.MAX_APK_BYTES
        ) throw IOException("The Manager update offer is not valid")
        val directory = File(context.cacheDir.canonicalFile, "manager_updates")
        if ((!directory.isDirectory && !directory.mkdirs()) ||
            directory.canonicalFile.parentFile != context.cacheDir.canonicalFile
        ) throw IOException("Cannot create private Manager update storage")
        val target = File(directory, release.assetName)
        val job = currentCoroutineContext()
        if (target.exists()) {
            try {
                val hash = validateSaved(context, target, release, null)
                withContext(Dispatchers.Main) { onProgress(100) }
                return target to hash
            } catch (error: IOException) {
                if (!target.delete()) throw IOException("Cannot remove an invalid saved update", error)
            }
        }
        directory.listFiles()?.forEach { file ->
            if (file != target && file.isFile &&
                file.canonicalFile.parentFile == directory.canonicalFile &&
                System.currentTimeMillis() - file.lastModified() > TimeUnit.DAYS.toMillis(1)
            ) file.delete()
        }
        val temporary = File.createTempFile("manager-", ".apk", directory)
        var published = false
        try {
            val hash = download(release, temporary, onProgress)
            job.ensureActive()
            validateSaved(context, temporary, release, hash)
            job.ensureActive()
            if (!temporary.renameTo(target)) throw IOException("Cannot publish the verified update")
            published = true
            return target to hash
        } finally {
            if (!published) temporary.delete()
        }
    }

    @Suppress("DEPRECATION")
    private suspend fun validateSaved(
        context: Context,
        file: File,
        release: LatestVersionInfo,
        pinnedHash: String?,
    ): String {
        if (file.canonicalFile != file.absoluteFile) throw IOException("The update file is not private")
        val coroutine = currentCoroutineContext()
        val hash = CoreManagerUpdatePolicy.verifyFile(file, release, pinnedHash, coroutine::ensureActive)
        if (ManagerHiding.isHidden) {
            ManagerHiding.validateOfficialUpdate(file, release)
            return CoreManagerUpdatePolicy.verifyFile(file, release, hash, coroutine::ensureActive)
        }
        val current = identity(installed(context))
        val archive = context.packageManager.getPackageArchiveInfo(
            file.absolutePath, PackageManager.GET_SIGNING_CERTIFICATES,
        ) ?: throw IOException("The downloaded file is not an installable APK")
        CoreManagerUpdatePolicy.validateIdentity(current, identity(archive), release)
        CoreManagerUpdatePolicy.verifyFile(file, release, hash, coroutine::ensureActive)
        return hash
    }

    private sealed interface Hop {
        data class Redirect(val url: String) : Hop
        data class Complete(val hash: String) : Hop
    }

    private suspend fun download(
        release: LatestVersionInfo,
        destination: File,
        onProgress: (Int) -> Unit,
    ): String {
        val client = ksuApp.okhttpClient.newBuilder()
            .cache(null)
            .followRedirects(false)
            .followSslRedirects(false)
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .callTimeout(120, TimeUnit.SECONDS)
            .build()
        var url = release.downloadUrl
        repeat(6) {
            if (!RegistryUrls.isAllowedDownloadHop(url)) throw IOException("The update download host is not allowed")
            when (val result = downloadHop(client, url, destination, release, onProgress)) {
                is Hop.Complete -> return result.hash
                is Hop.Redirect -> url = result.url
            }
        }
        throw IOException("Too many update download redirects")
    }

    private suspend fun downloadHop(
        client: OkHttpClient,
        url: String,
        destination: File,
        release: LatestVersionInfo,
        onProgress: (Int) -> Unit,
    ): Hop = suspendCancellableCoroutine { continuation ->
        val call = client.newCall(Request.Builder().url(url)
            .header("Accept", "application/octet-stream")
            .header("Accept-Encoding", "identity").build())
        continuation.invokeOnCancellation {
            call.cancel()
            destination.delete()
        }
        call.enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                if (continuation.isActive) continuation.resumeWithException(e)
            }

            override fun onResponse(call: Call, response: Response) {
                try {
                    val result = response.use {
                        if (response.code in listOf(301, 302, 303, 307, 308)) {
                            val next = response.header("Location")?.let(response.request.url::resolve)
                                ?: throw IOException("The update redirect has no destination")
                            if (!RegistryUrls.isAllowedDownloadHop(next.toString())) {
                                throw IOException("The update redirect host is not allowed")
                            }
                            Hop.Redirect(next.toString())
                        } else {
                            if (response.code != 200) throw IOException("Update download HTTP ${response.code}")
                            val length = response.body.contentLength()
                            if (length >= 0 && length != release.assetSize) {
                                throw IOException("The APK response size does not match the release")
                            }
                            var previous = -1
                            val hash = CoreManagerUpdatePolicy.copyDownload(
                                response.body.byteStream(), destination, release,
                                checkActive = {
                                    if (!continuation.isActive) throw IOException("Update download cancelled")
                                },
                                onProgress = { bytes ->
                                    val percent = (bytes * 100 / release.assetSize).toInt()
                                    if (percent != previous) {
                                        previous = percent
                                        mainHandler.post { if (continuation.isActive) onProgress(percent) }
                                    }
                                },
                            )
                            Hop.Complete(hash)
                        }
                    }
                    continuation.resume(result)
                } catch (error: Exception) {
                    destination.delete()
                    if (continuation.isActive) continuation.resumeWithException(error)
                }
            }
        })
    }
}
