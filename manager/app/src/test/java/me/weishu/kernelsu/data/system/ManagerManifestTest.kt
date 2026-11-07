package me.weishu.kernelsu.data.system

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

class ManagerManifestTest {
    private val source = "io.xkcnsu.manager"
    private val target = "com.example.application"

    @Test
    fun utf8AndUtf16KeepClassNamesAndResourceReferences() {
        for (utf8 in listOf(true, false)) {
            val values = listOf(source, "$source.fileprovider", "$source.webui",
                "me.weishu.kernelsu.KernelSUApplication", "android.permission.INTERNET")
            val rewritten = ManagerManifest.rename(manifest(values, utf8), source, target)
            assertEquals(rewritten.size, ByteBuffer.wrap(rewritten).order(ByteOrder.LITTLE_ENDIAN).getInt(4))
            assertEquals(listOf(target, "$target.fileprovider", "$target.webui",
                values[3], values[4]), strings(rewritten))
            assertArrayEquals(byteArrayOf(0x80.toByte(), 1, 8, 0, 8, 0, 0, 0), rewritten.takeLast(8).toByteArray())
        }
    }

    @Test
    fun longNamesAndUtf8MultibyteTextSurviveRoundTrip() {
        val longValue = "m".repeat(180)
        val values = listOf(source, longValue, "\u6a21\u5757", "$source.fileprovider")
        for (utf8 in listOf(true, false)) {
            val renamed = ManagerManifest.rename(manifest(values, utf8), source, target)
            val restored = ManagerManifest.rename(renamed, target, source)
            assertEquals(values, strings(restored))
        }
    }

    @Test
    fun malformedChunksAndMissingPackageAreRejected() {
        val input = manifest(listOf(source), true)
        assertThrows(IllegalArgumentException::class.java) {
            ManagerManifest.rename(input.copyOf(input.size - 1), source, target)
        }
        val badSize = input.copyOf().also {
            ByteBuffer.wrap(it).order(ByteOrder.LITTLE_ENDIAN).putInt(12, Int.MAX_VALUE)
        }
        assertThrows(IllegalArgumentException::class.java) {
            ManagerManifest.rename(badSize, source, target)
        }
        assertThrows(IllegalArgumentException::class.java) {
            ManagerManifest.rename(manifest(listOf("other.package"), true), source, target)
        }
    }

    @Test
    fun packageNamesCannotEscapePrivatePaths() {
        assertTrue(ManagerManifest.validPackage(target))
        for (value in listOf("", ".example", "one", "../data", "com..app", "com.1app",
            "com.app/", "com.app\u0000", "com.App", "x".repeat(201))) {
            assertFalse(value, ManagerManifest.validPackage(value))
        }
    }

    @Test
    fun aPackageMatchingTheJavaNamespaceKeepsComponentClasses() {
        val codePackage = "me.weishu.kernelsu"
        val values = listOf(codePackage, "$codePackage.fileprovider", "$codePackage.webui",
            "$codePackage.KernelSUApplication", "$codePackage.ui.MainActivity",
            "$codePackage.magica.AppZygotePreload")
        val rewritten = ManagerManifest.rename(manifest(values, true), codePackage, target)
        assertEquals(listOf(target, "$target.fileprovider", "$target.webui",
            values[3], values[4], values[5]), strings(rewritten))
    }

    @Test
    fun personalizedUnicodeLabelUsesAStringWithoutChangingComponentNames() {
        for (utf8 in listOf(true, false)) {
            val values = listOf(source, "application", "label", "http://schemas.android.com/apk/res/android",
                "me.weishu.kernelsu.KernelSUApplication")
            val plain = manifest(values, utf8)
            val app = ByteBuffer.allocate(56).order(ByteOrder.LITTLE_ENDIAN).apply {
                putShort(0x102); putShort(16); putInt(56)
                putInt(1); putInt(-1)
                putInt(-1); putInt(1)
                putShort(20); putShort(20); putShort(1)
                putShort(0); putShort(0); putShort(0)
                putInt(3); putInt(2); putInt(-1)
                putShort(8); put(0); put(1); putInt(0x7f010000)
            }.array()
            val input = (plain + app).also { ByteBuffer.wrap(it).order(ByteOrder.LITTLE_ENDIAN).putInt(4, it.size) }
            val label = "\u6211\u7684\u5de5\u5177 \uD83D\uDD27"
            val rewritten = ManagerManifest.rename(input, source, target, label)
            assertEquals(values.size + 1, strings(rewritten).size)
            assertEquals(label, strings(rewritten).last())
            assertEquals(values.last(), strings(rewritten)[4])
            val data = ByteBuffer.wrap(rewritten).order(ByteOrder.LITTLE_ENDIAN)
            val at = rewritten.size - 56 + 36
            assertEquals(values.size, data.getInt(at + 8))
            assertEquals(3, data.get(at + 15).toInt())
            assertEquals(values.size, data.getInt(at + 16))
        }
    }

    @Test
    fun labelsCannotBeSilentlyIgnoredWhenApplicationAttributeIsMissing() {
        assertThrows(IllegalArgumentException::class.java) {
            ManagerManifest.rename(manifest(listOf(source), true), source, target, "Tools")
        }
    }

    private fun manifest(values: List<String>, utf8: Boolean): ByteArray {
        val data = ByteArrayOutputStream()
        val offsets = values.map { value ->
            val offset = data.size()
            if (utf8) {
                val encoded = value.toByteArray(Charsets.UTF_8)
                for (length in listOf(value.length, encoded.size)) {
                    if (length >= 128) data.write((length shr 8) or 128)
                    data.write(length and 255)
                }
                data.write(encoded)
                data.write(0)
            } else {
                data.write(value.length and 255)
                data.write(value.length shr 8)
                data.write(value.toByteArray(Charsets.UTF_16LE))
                data.write(byteArrayOf(0, 0))
            }
            offset
        }
        while (data.size() % 4 != 0) data.write(0)
        val start = 28 + offsets.size * 4
        val pool = ByteBuffer.allocate(start + data.size()).order(ByteOrder.LITTLE_ENDIAN).apply {
            putShort(1); putShort(28); putInt(capacity())
            putInt(offsets.size); putInt(0); putInt(if (utf8) 0x101 else 1)
            putInt(start); putInt(0)
            offsets.forEach(::putInt)
            put(data.toByteArray())
        }.array()
        return ByteBuffer.allocate(8 + pool.size + 8).order(ByteOrder.LITTLE_ENDIAN).apply {
            putShort(3); putShort(8); putInt(capacity())
            put(pool)
            putShort(0x180); putShort(8); putInt(8)
        }.array()
    }

    private fun strings(xml: ByteArray): List<String> {
        val data = ByteBuffer.wrap(xml).order(ByteOrder.LITTLE_ENDIAN)
        val header = data.getShort(10).toInt() and 0xffff
        val count = data.getInt(16)
        val utf8 = data.getInt(24) and 0x100 != 0
        val start = data.getInt(28) + 8
        fun length(): Int {
            if (!utf8) return data.short.toInt() and 0xffff
            val first = data.get().toInt() and 255
            return if (first < 128) first else ((first and 127) shl 8) or (data.get().toInt() and 255)
        }
        return List(count) { index ->
            data.position(start + data.getInt(8 + header + index * 4))
            if (utf8) length()
            val size = length() * if (utf8) 1 else 2
            val value = ByteArray(size).also(data::get)
            value.toString(if (utf8) Charsets.UTF_8 else Charsets.UTF_16LE)
        }
    }
}
