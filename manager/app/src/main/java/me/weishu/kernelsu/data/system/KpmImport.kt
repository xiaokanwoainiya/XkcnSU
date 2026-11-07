package me.weishu.kernelsu.data.system

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.OpenableColumns
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File

object KpmImport {
    suspend fun importDocument(context: Context, uri: Uri, repository: KpmRepository): KpmImportResult =
        withContext(Dispatchers.IO) {
            val resolver = context.contentResolver
            var acquiredPermission = false
            var staged: File? = null
            try {
                if (uri.scheme != "content") throw BackendException("invalid_uri", "Select a document")
                val alreadyGranted = resolver.persistedUriPermissions.any { it.uri == uri && it.isReadPermission }
                if (!alreadyGranted) {
                    acquiredPermission = runCatching {
                        resolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)
                        true
                    }.getOrDefault(false)
                }
                val name = resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { cursor ->
                    if (cursor.moveToFirst()) cursor.getString(0) else null
                }
                if (name != null && !name.endsWith(".kpm", ignoreCase = true)) {
                    throw BackendException("invalid_file", "Select a .kpm file")
                }
                val file = File.createTempFile("xkcnsu-kpm-", ".kpm", context.cacheDir)
                staged = file
                val input = resolver.openInputStream(uri)
                    ?: throw BackendException("read_failed", "Cannot open the selected document")
                input.use { source -> file.outputStream().use { KpmDocument.copyBounded(source, it) } }
                repository.importModule(file.absolutePath)
            } finally {
                staged?.delete()
                if (acquiredPermission) {
                    runCatching { resolver.releasePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION) }
                }
            }
        }
}
