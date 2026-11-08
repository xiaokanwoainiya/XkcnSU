package me.weishu.kernelsu.magica

import android.content.Context
import android.os.Build
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull

/** Built-in GhostLock path. Profiles are generated at build time and selected by
 * exact uname; runtime never guesses offsets or parses incomplete profiles. */
object GhostlockRunner {
    data class Result(val code: Int, val message: String, val log: File? = null)

    private const val TIMEOUT_MS = 5 * 60 * 1000L

    suspend fun run(context: Context): Result = withContext(Dispatchers.IO) {
        val kernel = runCatching { File("/proc/sys/kernel/osrelease").readText().trim() }
            .getOrElse { return@withContext Result(1, "无法读取当前内核版本") }
        if (Build.SUPPORTED_ABIS.none { it == "arm64-v8a" }) {
            return@withContext Result(2, "GhostLock 仅支持 arm64-v8a")
        }
        val binary = File(context.applicationInfo.nativeLibraryDir, "libghostlock.so")
        if (!binary.isFile) return@withContext Result(3, "内置 GhostLock native 缺失")

        val name = "$kernel.bin"
        val work = File(context.filesDir, "ghostlock").apply { mkdirs() }
        val profileFile = File(work, name)
        val copied = runCatching {
            context.assets.open("ghostlock/profiles-bin/$name").use { input ->
                profileFile.outputStream().use { output -> input.copyTo(output) }
            }
            profileFile.isFile && profileFile.length() > 0L
        }.getOrDefault(false)
        if (!copied) {
            profileFile.delete()
            return@withContext Result(4, "当前内核没有匹配 profile：$kernel")
        }

        val log = File(work, "run-${System.currentTimeMillis()}.log")
        val process = try {
            ProcessBuilder(binary.absolutePath, "--ghostlock-app-call")
                .directory(work)
                .redirectErrorStream(true)
                .redirectOutput(log)
                .start()
        } catch (_: IOException) {
            return@withContext Result(5, "无法启动内置 GhostLock", log)
        }
        try {
            profileFile.inputStream().use { input ->
                process.outputStream.use { output -> input.copyTo(output) }
            }
            val finished = withTimeoutOrNull(TIMEOUT_MS) {
                process.waitFor(TIMEOUT_MS, TimeUnit.MILLISECONDS)
            } ?: false
            if (!finished) {
                process.destroy()
                if (!process.waitFor(2, TimeUnit.SECONDS)) process.destroyForcibly()
                return@withContext Result(6, "GhostLock 执行超时，日志：${log.name}", log)
            }
            val code = process.exitValue()
            Result(code, if (code == 0) "临时 Root 执行完成" else "GhostLock 执行失败：${log.name}", log)
        } catch (_: IOException) {
            process.destroyForcibly()
            Result(7, "GhostLock 输入输出失败，日志：${log.name}", log)
        } finally {
            profileFile.delete()
        }
    }
}
