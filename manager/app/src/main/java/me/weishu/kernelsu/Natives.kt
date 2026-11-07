package me.weishu.kernelsu

import android.os.Parcelable
import android.os.Build
import androidx.annotation.Keep
import androidx.compose.runtime.Immutable
import kotlinx.parcelize.Parcelize
import kotlinx.serialization.Serializable
import me.weishu.kernelsu.Natives.Profile.RootProfileFlag
import me.weishu.kernelsu.ui.util.rootAvailable

/**
 * @author weishu
 * @date 2022/12/8.
 */
object Natives {
    // minimal supported kernel version
    // 10915: allowlist breaking change, add app profile
    // 10931: app profile struct add 'version' field
    // 10946: add capabilities
    // 10977: change groups_count and groups to avoid overflow write
    // 11071: Fix the issue of failing to set a custom SELinux type.
    // 12143: breaking: new supercall impl
    // 32310: new get_allow_list ioctl
    // 32336: new set_sepolicy ioctl
    // 32377: add set_init_pgrp ioctl
    // 32513: add uapi version
    const val MINIMAL_SUPPORTED_KERNEL = 32513

    const val KERNEL_SU_DOMAIN = "u:r:ksu:s0"

    const val ROOT_UID = 0
    const val ROOT_GID = 0

    val loadError: LinkageError? = try {
        System.loadLibrary("kernelsu")
        null
    } catch (error: LinkageError) {
        runCatching {
            android.util.Log.e("XKCNSU", "Native bridge unavailable: ${Build.SUPPORTED_ABIS?.joinToString()}", error)
        }
        error
    }

    val isLoaded: Boolean
        get() = loadError == null

    private inline fun <T> nativeOr(fallback: T, block: () -> T): T =
        try {
            if (loadError != null) fallback else block()
        } catch (_: LinkageError) {
            fallback
        }

    val version: Int
        get() = nativeOr(0) { NativeBridge.version }

    val isSafeMode: Boolean
        get() = nativeOr(false) { NativeBridge.isSafeMode }

    val isLkmMode: Boolean
        get() = nativeOr(false) { NativeBridge.isLkmMode }

    val isLkmBundled: Boolean
        get() = nativeOr(false) { NativeBridge.isLkmBundled }

    val isLateLoadMode: Boolean
        get() = nativeOr(false) { NativeBridge.isLateLoadMode }

    val isManager: Boolean
        get() = nativeOr(false) { NativeBridge.isManager }

    val isManagerHideSupported: Boolean
        get() = nativeOr(false) { NativeBridge.isManagerHideSupported }

    fun verifyManagerAccess(): Boolean = nativeOr(false) { NativeBridge.verifyManagerAccess() }

    val isPrBuild: Boolean
        get() = nativeOr(false) { NativeBridge.isPrBuild }

    fun uidShouldUmount(uid: Int): Boolean = nativeOr(true) { NativeBridge.uidShouldUmount(uid) }

    /**
     * Get the profile of the given package.
     * @param key usually the package name
     * @return return null if failed.
     */
    fun getAppProfile(key: String?, uid: Int): Profile =
        nativeOr(Profile(key.orEmpty(), uid)) { NativeBridge.getAppProfile(key, uid) }
    fun setAppProfile(profile: Profile?): Boolean = nativeOr(false) { NativeBridge.setAppProfile(profile) }

    /**
     * `su` compat mode can be disabled temporarily.
     *  0: disabled
     *  1: enabled
     *  negative : error
     */
    fun isSuEnabled(): Boolean = nativeOr(false) { NativeBridge.isSuEnabled() }
    fun setSuEnabled(enabled: Boolean): Boolean = nativeOr(false) { NativeBridge.setSuEnabled(enabled) }

    /**
     * Kernel module umount can be disabled temporarily.
     *  0: disabled
     *  1: enabled
     *  negative : error
     */
    fun isKernelUmountEnabled(): Boolean = nativeOr(false) { NativeBridge.isKernelUmountEnabled() }
    fun setKernelUmountEnabled(enabled: Boolean): Boolean =
        nativeOr(false) { NativeBridge.setKernelUmountEnabled(enabled) }

    /**
     * SELinux hide can be disabled temporarily.
     *  0: disabled
     *  1: enabled
     *  negative : error
     */
    fun isSelinuxHideEnabled(): Boolean = nativeOr(false) { NativeBridge.isSelinuxHideEnabled() }
    fun setSelinuxHideEnabled(enabled: Boolean): Int = nativeOr(-19) { NativeBridge.setSelinuxHideEnabled(enabled) }

    /**
     * Get the user name for the uid.
     */
    fun getUserName(uid: Int): String? = nativeOr<String?>(null) { NativeBridge.getUserName(uid) }

    fun getSuperuserCount(): Int = nativeOr(0) { NativeBridge.getSuperuserCount() }

    private const val NON_ROOT_DEFAULT_PROFILE_KEY = "$"
    private const val NOBODY_UID = 9999

    fun setDefaultUmountModules(umountModules: Boolean): Boolean {
        Profile(
            NON_ROOT_DEFAULT_PROFILE_KEY,
            NOBODY_UID,
            false,
            umountModules = umountModules
        ).let {
            return setAppProfile(it)
        }
    }

    fun isDefaultUmountModules(): Boolean {
        getAppProfile(NON_ROOT_DEFAULT_PROFILE_KEY, NOBODY_UID).let {
            return it.umountModules
        }
    }

    val kernelUAPIVersion: Int
        get() = nativeOr(0) { NativeBridge.kernelUAPIVersion }

    val managerUAPIVersion: Int
        get() = nativeOr(0) { NativeBridge.managerUAPIVersion }

    fun isFullFeatured(): Boolean {
        return isManager && kernelUAPIVersion == managerUAPIVersion && rootAvailable()
    }

    @Keep
    @Immutable
    @Parcelize
    @Serializable
    data class Profile(
        // and there is a default profile for root and non-root
        val name: String,
        // current uid for the package, this is convivent for kernel to check
        // if the package name doesn't match uid, then it should be invalidated.
        val currentUid: Int = 0,

        // if this is true, kernel will grant root permission to this package
        val allowSu: Boolean = false,

        // these are used for root profile
        val rootUseDefault: Boolean = true,
        val rootTemplate: String? = null,
        val uid: Int = ROOT_UID,
        val gid: Int = ROOT_GID,
        val groups: List<Int> = mutableListOf(),
        val capabilities: List<Int> = mutableListOf(),
        val context: String = KERNEL_SU_DOMAIN,
        val namespace: Int = Namespace.INHERITED.ordinal,

        val nonRootUseDefault: Boolean = true,
        val umountModules: Boolean = true,
        var rules: String = "", // this field is save in ksud!!

        val flags: Long = FLAG_KSU_NO_NEW_PRIVS,
    ) : Parcelable {
        @Keep
        enum class RootProfileFlag(val display: String, val desc: Int) {
            NO_NEW_PRIVS(
                "NO_NEW_PRIVS",
                R.string.profile_flags_desc_no_new_privs
            )
        }

        enum class Namespace {
            INHERITED,
            GLOBAL,
            INDIVIDUAL,
        }

        constructor() : this("")
    }

    const val FLAG_KSU_NO_NEW_PRIVS = 1L
}

fun List<RootProfileFlag>.toRawFlags(): Long =
    fold(0L) { acc, flag -> acc.or(1L.shl(flag.ordinal)) }

fun List<RootProfileFlag>.toOrdinalList(): List<Int> =
    map { it.ordinal }

fun Long.toRootProfileFlags(): List<RootProfileFlag> =
    RootProfileFlag.entries.filter { 1L.shl(it.ordinal).and(this) != 0L }.toList()
