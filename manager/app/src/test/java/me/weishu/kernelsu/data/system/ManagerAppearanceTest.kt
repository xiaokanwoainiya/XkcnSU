package me.weishu.kernelsu.data.system

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

class ManagerAppearanceTest {
    @Test
    fun namesSupportUnicodeAndRejectEmptyControlOrBidiLabels() {
        assertEquals("Tools", ManagerAppearance.validateName("  Tools "))
        assertEquals("\u6211\u7684\u5de5\u5177", ManagerAppearance.validateName("\u6211\u7684\u5de5\u5177"))
        assertEquals("\uD83D\uDD27".repeat(40), ManagerAppearance.validateName("\uD83D\uDD27".repeat(40)))
        for (name in listOf("", "  ", "x".repeat(41), "Tools\nHidden", "x\u0000y", "x\u202Ey", "\uD800")) {
            assertThrows(IllegalArgumentException::class.java) { ManagerAppearance.validateName(name) }
        }
        assertThrows(IllegalArgumentException::class.java) { ManagerAppearance(" Untrimmed ") }
        assertThrows(IllegalArgumentException::class.java) { ManagerAppearance("Tools", "res/../private.png") }
    }

    @Test
    fun resourceStringRewritePreservesIdsPackageChunkAndUnrelatedStrings() {
        val values = listOf("XKCNSU", "unrelated", "res/a.png", "prefix XKCNSU suffix")
        val pool = stringPool(values)
        val suffix = byteArrayOf(0, 2, 8, 0, 12, 0, 0, 0, 1, 0, 0, 0)
        val table = ByteBuffer.allocate(12 + pool.size + suffix.size).order(ByteOrder.LITTLE_ENDIAN).apply {
            putShort(2); putShort(12); putInt(capacity()); putInt(1); put(pool); put(suffix)
        }.array()
        val renamed = ManagerResources.renameLabel(table, "XKCNSU", "\u6211\u7684\u5de5\u5177")
        val size = ByteBuffer.wrap(renamed).order(ByteOrder.LITTLE_ENDIAN).getInt(16)
        assertEquals(listOf("\u6211\u7684\u5de5\u5177") + values.drop(1),
            ManagerStringPool.strings(renamed.copyOfRange(12, 12 + size)))
        assertEquals(suffix.toList(), renamed.takeLast(suffix.size))
        assertEquals(renamed.size, ByteBuffer.wrap(renamed).order(ByteOrder.LITTLE_ENDIAN).getInt(4))
        val reverse = ManagerResources.renameLabel(renamed, "\u6211\u7684\u5de5\u5177", "XKCNSU")
        assertEquals(values, ManagerStringPool.strings(reverse.copyOfRange(12, 12 + pool.size)))
    }

    @Test
    fun appendedPoolEntriesKeepEveryExistingIndex() {
        val values = listOf("package", "\uD83D\uDD27", "x".repeat(150))
        val rewritten = ManagerStringPool.rewrite(stringPool(values), listOf("New Name"))
        assertEquals(values + "New Name", ManagerStringPool.strings(rewritten))
    }

    @Test
    fun styledStringsKeepValidatedSpansAndCannotBeSilentlyShortened() {
        val plain = stringPool(listOf("XKCNSU", "b"))
        val dataStart = 36
        val data = plain.copyOfRange(dataStart, plain.size)
        val styled = ByteBuffer.allocate(40 + data.size + 24).order(ByteOrder.LITTLE_ENDIAN).apply {
            put(plain, 0, 28)
            putInt(4, capacity()); putInt(12, 1); putInt(20, 40); putInt(24, 40 + data.size)
            position(28)
            put(plain, 28, 8)
            putInt(0)
            put(data)
            putInt(1); putInt(0); putInt(5)
            putInt(-1); putInt(-1); putInt(-1)
        }.array()
        assertEquals(listOf("XKCNSU", "b", "Extra"),
            ManagerStringPool.strings(ManagerStringPool.rewrite(styled, listOf("Extra"))))
        assertThrows(IllegalArgumentException::class.java) {
            ManagerStringPool.rewrite(styled) { if (it == "XKCNSU") "A" else it }
        }
        val invalid = styled.copyOf().also {
            ByteBuffer.wrap(it).order(ByteOrder.LITTLE_ENDIAN).putInt(40 + data.size, 99)
        }
        assertThrows(IllegalArgumentException::class.java) { ManagerStringPool.rewrite(invalid) }
    }

    @Test
    fun unrelatedExtendedUtf16StringSurvivesRewriting() {
        val value = "x".repeat(33_000)
        val encoded = value.toByteArray(Charsets.UTF_16LE)
        val size = (32 + 4 + encoded.size + 2 + 3) / 4 * 4
        val pool = ByteBuffer.allocate(size).order(ByteOrder.LITTLE_ENDIAN).apply {
            putShort(1); putShort(28); putInt(capacity())
            putInt(1); putInt(0); putInt(0); putInt(32); putInt(0); putInt(0)
            putShort(0x8000.toShort()); putShort(value.length.toShort())
            put(encoded); putShort(0)
        }.array()
        assertEquals(listOf(value), ManagerStringPool.strings(ManagerStringPool.rewrite(pool)))
    }

    private fun stringPool(values: List<String>): ByteArray {
        val data = ByteArrayOutputStream()
        val offsets = values.map { value ->
            val offset = data.size()
            val bytes = value.toByteArray(Charsets.UTF_8)
            for (length in listOf(value.length, bytes.size)) {
                if (length >= 128) data.write((length shr 8) or 128)
                data.write(length and 255)
            }
            data.write(bytes)
            data.write(0)
            offset
        }
        while (data.size() % 4 != 0) data.write(0)
        val start = 28 + offsets.size * 4
        return ByteBuffer.allocate(start + data.size()).order(ByteOrder.LITTLE_ENDIAN).apply {
            putShort(1); putShort(28); putInt(capacity())
            putInt(values.size); putInt(0); putInt(0x100); putInt(start); putInt(0)
            offsets.forEach(::putInt)
            put(data.toByteArray())
        }.array()
    }
}
