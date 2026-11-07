package me.weishu.kernelsu.data.system

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test
import java.nio.ByteBuffer
import java.nio.ByteOrder

class ManagerTransferTest {
    private val transfer = ManagerTransfer(
        "a".repeat(64), "io.xkcnsu.manager", "com.example.application",
        10333, 10334, 32801, "0.2.1", "b".repeat(64), "c".repeat(64), true, 1_000_000L,
    )

    @Test
    fun onlyMatchingNoncePackageUidAndFreshTransferAreAccepted() {
        transfer.validate(transfer.token, transfer.target, transfer.targetUid, transfer.createdAt + 1)
        for (invalid in listOf(
            transfer.copy(token = "invalid"),
            transfer.copy(target = transfer.source),
            transfer.copy(source = "../private"),
            transfer.copy(sourceUid = transfer.targetUid),
            transfer.copy(sourceUid = 0),
            transfer.copy(targetUid = 0),
            transfer.copy(targetCertificate = "f".repeat(63)),
            transfer.copy(sourceCertificate = "x".repeat(64)),
            transfer.copy(versionCode = 0),
            transfer.copy(versionName = ""),
        )) assertThrows(IllegalArgumentException::class.java) {
            invalid.validate(transfer.token, transfer.target, transfer.targetUid, transfer.createdAt)
        }
        for (nonce in listOf("", "b".repeat(64), transfer.token + "a")) {
            assertThrows(IllegalArgumentException::class.java) {
                transfer.validate(nonce, transfer.target, transfer.targetUid, transfer.createdAt)
            }
        }
        for (time in listOf(transfer.createdAt - 1, transfer.createdAt + 180_001)) {
            assertThrows(IllegalArgumentException::class.java) {
                transfer.validate(transfer.token, transfer.target, transfer.targetUid, time)
            }
        }
        assertThrows(IllegalArgumentException::class.java) {
            transfer.validate(transfer.token, transfer.source, transfer.targetUid, transfer.createdAt)
        }
        assertThrows(IllegalArgumentException::class.java) {
            transfer.validate(transfer.token, transfer.target, transfer.sourceUid, transfer.createdAt)
        }
    }

    @Test
    fun enrollmentMatchesPackedKernelContract() {
        val record = transfer.identityRecord(695)
        assertEquals(333, record.size)
        val bytes = ByteBuffer.wrap(record).order(ByteOrder.LITTLE_ENDIAN)
        assertEquals(0x584b4849, bytes.int)
        assertEquals(1, bytes.int)
        assertEquals(695, bytes.int)
        assertEquals(transfer.target, record.copyOfRange(12, 268)
            .toString(Charsets.US_ASCII).substringBefore('\u0000'))
        assertEquals(0, record[267].toInt())
        assertEquals(transfer.targetCertificate, record.copyOfRange(268, 332).toString(Charsets.US_ASCII))
        assertEquals(0, record[332].toInt())
        for (size in listOf(0, -1, 1025)) assertThrows(IllegalArgumentException::class.java) {
            transfer.identityRecord(size)
        }
        assertThrows(IllegalArgumentException::class.java) {
            transfer.copy(hidden = false).identityRecord(695)
        }
    }
}
