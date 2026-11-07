package me.weishu.kernelsu.data.system

import kotlinx.serialization.Serializable
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest

@Serializable
data class ManagerTransfer(
    val token: String,
    val source: String,
    val target: String,
    val sourceUid: Int,
    val targetUid: Int,
    val versionCode: Long,
    val versionName: String,
    val sourceCertificate: String,
    val targetCertificate: String,
    val hidden: Boolean,
    val createdAt: Long,
) {
    fun validate(nonce: String, currentPackage: String, currentUid: Int, now: Long) {
        require(Regex("[0-9a-f]{64}").matches(token) &&
            MessageDigest.isEqual(token.toByteArray(), nonce.toByteArray())) { "Invalid manager transfer" }
        require(ManagerManifest.validPackage(source) && ManagerManifest.validPackage(target) && source != target)
        require(target == currentPackage && targetUid == currentUid &&
            sourceUid in 10000..99999 && targetUid in 10000..99999 && sourceUid != targetUid)
        require(now - createdAt in 0..180_000L && versionCode > 0 && versionName.isNotEmpty()) {
            "Manager transfer expired"
        }
        require(listOf(sourceCertificate, targetCertificate).all { Regex("[0-9a-f]{64}").matches(it) })
    }

    fun identityRecord(certificateSize: Int): ByteArray {
        require(certificateSize in 1..1024 && ManagerManifest.validPackage(target) && hidden &&
            Regex("[0-9a-f]{64}").matches(targetCertificate))
        return ByteBuffer.allocate(333).order(ByteOrder.LITTLE_ENDIAN).apply {
            putInt(0x584b4849) // XKHI; must match KSU_MANAGER_IDENTITY_MAGIC.
            putInt(1)
            putInt(certificateSize)
            put(target.toByteArray(Charsets.US_ASCII))
            position(12 + 256)
            put(targetCertificate.toByteArray(Charsets.US_ASCII))
        }.array()
    }
}
