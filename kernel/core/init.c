#include <linux/export.h>
#include <linux/fs.h>
#include <linux/module.h>
#include <linux/rcupdate.h>
#include <linux/sched.h>
#include <linux/workqueue.h>
#include <linux/moduleparam.h>

#include "policy/allowlist.h"
#include "policy/app_profile.h"
#include "policy/feature.h"
#include "klog.h" // IWYU pragma: keep
#include "manager/manager_observer.h"
#include "manager/throne_tracker.h"
#include "hook/syscall_hook_manager.h"
#include "hook/lsm_hook.h"
#include "runtime/ksud.h"
#include "runtime/ksud_boot.h"
#include "feature/sulog.h"
#include "supercall/supercall.h"
#include "ksu.h"
#include "infra/file_wrapper.h"
#include "selinux/selinux.h"
#include "hook/syscall_hook.h"
#include "hook/tp_marker.h"
#include "feature/adb_root.h"
#include "feature/selinux_hide.h"
#include "feature/avc_spoof.h"
#include "feature/module_blacklist.h"
#include "feature/shell_su_adb_only.h"
#include "infra/symbol_resolver.h"
#include "preinit/preinit.h"
#include "xkcnsu_root.h"

#ifndef CONFIG_ARM64
#error "XKCNSU's first release requires ARM64 GKI"
#endif

#if defined(__x86_64__) && !defined(CONFIG_KSU_X86_PATCH_SYSCALL_DISPATCHER)
#include <asm/cpufeature.h>
#include <linux/version.h>
#ifndef X86_FEATURE_INDIRECT_SAFE
#error "FATAL: Your kernel is missing the indirect syscall bypass patches!"
#endif
#endif

// workaround for A12-5.10 kernel
// Some third-party kernel (e.g. linegaeOS) uses wrong toolchain, which supports
// CC_HAVE_STACKPROTECTOR_SYSREG while gki's toolchain doesn't.
// Therefore, ksu lkm, which uses gki toolchain, requires this __stack_chk_guard,
// while those third-party kernel can't provide.
// Thus, we manually provide it instead of using kernel's
#if defined(CONFIG_STACKPROTECTOR) &&                                                                                  \
    (defined(CONFIG_ARM64) && defined(MODULE) && !defined(CONFIG_STACKPROTECTOR_PER_TASK))
#include <linux/stackprotector.h>
#include <linux/random.h>
unsigned long __stack_chk_guard __ro_after_init __attribute__((visibility("hidden")));

__attribute__((no_stack_protector)) void __init ksu_setup_stack_chk_guard()
{
    unsigned long canary;

    /* Try to get a semi random initial value. */
    get_random_bytes(&canary, sizeof(canary));
    canary ^= LINUX_VERSION_CODE;
    canary &= CANARY_MASK;
    __stack_chk_guard = canary;
}

__attribute__((naked)) int __init kernelsu_init_early(void)
{
    asm("mov x19, x30;\n"
        "bl ksu_setup_stack_chk_guard;\n"
        "mov x30, x19;\n"
        "b kernelsu_init;\n");
}
#define NEED_OWN_STACKPROTECTOR 1
#else
#define NEED_OWN_STACKPROTECTOR 0
#endif

struct cred *ksu_cred;
bool ksu_late_loaded;

#ifdef CONFIG_KSU_DEBUG
bool allow_shell = true;
#else
bool allow_shell = false;
#endif
module_param(allow_shell, bool, 0);

bool ksu_no_custom_rc = false;
module_param_named(norc, ksu_no_custom_rc, bool, 0);

#ifdef MODULE
bool ksu_bundled = false;
module_param_named(bundled, ksu_bundled, bool, 0);
#endif

static bool feature_initialized, allowlist_initialized, sulog_initialized;
static bool adb_initialized, shell_initialized, manager_initialized;
static bool backend_initialized, supercalls_initialized, runtime_initialized;
static bool preinit_initialized, throne_initialized, observer_initialized;
static bool hide_initialized, avc_initialized;
static int xkcnsu_init_error;
module_param_named(init_error, xkcnsu_init_error, int, 0444);
MODULE_PARM_DESC(init_error, "Initialization errno; nonzero means a quarantined, unready root provider");

static int xkcnsu_detach_hooks(void)
{
    int ret;

    xkcnsu_root_set_ready(false);
    if (avc_initialized) {
        ksu_avc_spoof_exit();
        avc_initialized = false;
    }
    if (hide_initialized) {
        ret = ksu_selinux_hide_exit();
        if (ret)
            return ret;
        hide_initialized = false;
    }
    if (runtime_initialized) {
        ret = ksu_ksud_exit();
        if (ret)
            return ret;
        runtime_initialized = false;
    }
    if (preinit_initialized) {
        ksu_preinit_exit();
        preinit_initialized = false;
    }
    if (manager_initialized) {
        ksu_syscall_hook_manager_exit();
        manager_initialized = false;
    }
    if (supercalls_initialized) {
        ksu_supercalls_exit();
        supercalls_initialized = false;
    }
    if (backend_initialized) {
        ret = ksu_syscall_hook_exit();
        if (ret)
            return ret;
        backend_initialized = false;
    }
    return ksu_lsm_hook_exit();
}

static void xkcnsu_release_state(void)
{
    if (observer_initialized)
        ksu_observer_exit();
    if (throne_initialized)
        ksu_throne_tracker_exit();
    if (shell_initialized)
        ksu_shell_su_adb_only_exit();
    if (adb_initialized)
        ksu_adb_root_exit();
    if (sulog_initialized)
        ksu_sulog_exit();
    if (allowlist_initialized)
        ksu_allowlist_exit();
    if (feature_initialized)
        ksu_feature_exit();
    ksu_module_blacklist_exit();
    put_cred(ksu_cred);
    ksu_cred = NULL;
}

int __init kernelsu_init(void)
{
    int ret;

    pr_info("welcome to XKCNSU version " __stringify(KERNEL_SU_VERSION) ", package name " KSU_PACKAGE_NAME "\n");
#if defined(__x86_64__) && !defined(CONFIG_KSU_X86_PATCH_SYSCALL_DISPATCHER)
    // If the kernel has the hardening patch, X86_FEATURE_INDIRECT_SAFE must be set
    if (!boot_cpu_has(X86_FEATURE_INDIRECT_SAFE)) {
        pr_alert("*************************************************************");
        pr_alert("**     NOTICE NOTICE NOTICE NOTICE NOTICE NOTICE NOTICE    **");
        pr_alert("**                                                         **");
        pr_alert("**        X86_FEATURE_INDIRECT_SAFE is not enabled!        **");
        pr_alert("**      KernelSU will abort initialization to prevent      **");
        pr_alert("**                     kernel panic.                       **");
        pr_alert("**                                                         **");
        pr_alert("**     NOTICE NOTICE NOTICE NOTICE NOTICE NOTICE NOTICE    **");
        pr_alert("*************************************************************");
        return -ENOSYS;
    }
#endif

#ifdef MODULE
    ksu_late_loaded = (current->pid != 1);
#else
    ksu_late_loaded = false;
#endif

#ifdef CONFIG_KSU_DEBUG
    pr_alert("*************************************************************");
    pr_alert("**     NOTICE NOTICE NOTICE NOTICE NOTICE NOTICE NOTICE    **");
    pr_alert("**                                                         **");
        pr_alert("**          You are running XKCNSU in DEBUG mode           **");
    pr_alert("**                                                         **");
    pr_alert("**     NOTICE NOTICE NOTICE NOTICE NOTICE NOTICE NOTICE    **");
    pr_alert("*************************************************************");
#endif
    if (allow_shell) {
        pr_alert("shell is allowed at init!");
    }

    ksu_cred = prepare_creds();
    if (!ksu_cred) {
        pr_err("prepare cred failed!\n");
        return -ENOSYS;
    }

    pr_info("initializing symbol resolver\n");
    ret = ksu_init_symbol_resolver();
    if (ret) {
        put_cred(ksu_cred);
        ksu_cred = NULL;
        return ret;
    }

    ret = ksu_selinux_inode_init();
    if (ret) {
        put_cred(ksu_cred);
        ksu_cred = NULL;
        return ret;
    }

    // Block modules early, before vendor modules are loaded in second stage.
    ret = ksu_module_blacklist_init();
    if (ret)
        goto fail;

    ksu_feature_init();
    feature_initialized = true;
    ksu_allowlist_init();
    allowlist_initialized = true;
    ret = ksu_sulog_init();
    if (ret)
        goto fail;
    sulog_initialized = true;
    ret = ksu_adb_root_init();
    if (ret)
        goto fail;
    adb_initialized = true;
    ksu_lsm_hook_init();
    ret = ksu_shell_su_adb_only_init();
    if (ret)
        goto fail;
    shell_initialized = true;

    ksu_app_profile_init();
    ksu_throne_tracker_init();
    throne_initialized = true;

    if (ksu_late_loaded) {
        pr_info("late load mode, skipping kprobe hooks\n");

        apply_kernelsu_rules();
        cache_sid();
        setup_ksu_cred();

        // Grant current process (ksud late-load) root
        // with KSU SELinux domain before enforcing SELinux, so it
        // can continue to access /data/app etc. after enforcement.
        escape_to_root_for_init();

        ksu_load_allow_list();

        ksu_boot_completed = true;

        if (!getenforce()) {
            pr_info("Permissive SELinux, enforcing\n");
            setenforce(true);
        }

    }

    backend_initialized = true;
    ret = ksu_syscall_hook_init();
    if (ret)
        goto fail;
    supercalls_initialized = true;
    ret = ksu_supercalls_init();
    if (ret)
        goto fail;
    ret = ksu_syscall_hook_manager_init();
    if (ret)
        goto fail;
    manager_initialized = true;

    if (!ksu_late_loaded) {
        ret = ksu_preinit_init();
        if (ret)
            goto fail;
        preinit_initialized = true;
        runtime_initialized = true;
        ret = ksu_ksud_init();
        if (ret)
            goto fail;
    }

    hide_initialized = true;
    ret = ksu_selinux_hide_init();
    if (ret)
        goto fail;
    ret = ksu_avc_spoof_init();
    if (ret)
        goto fail;
    avc_initialized = true;
    ret = ksu_file_wrapper_init();
    if (ret)
        goto fail;
    if (ksu_late_loaded) {
        ret = ksu_observer_init();
        if (ret)
            goto fail;
        observer_initialized = true;
        track_throne(false);
    }
    if (ksu_late_loaded) {
        ksu_feature_apply_forced();
        ksu_avc_spoof_late_init();
    }

    /* Raw table/LSM callbacks cannot veto delete_module from module_exit. */
    xkcnsu_root_pin_callbacks();
    xkcnsu_root_set_ready(true);
    ksu_allowlist_ready();
    ksu_mark_running_process();

#ifdef MODULE
#ifndef CONFIG_KSU_DEBUG
    kobject_del(&THIS_MODULE->mkobj.kobj);
#endif
#endif
    return 0;

fail:
    xkcnsu_init_error = ret;
    pr_err("XKCNSU initialization failed: %d\n", ret);
    {
        int cleanup_ret = xkcnsu_detach_hooks();

        if (cleanup_ret || xkcnsu_root_callbacks_pinned()) {
            /*
             * Returning an init error would free this module's text even with
             * references. Keep an inert, unready provider resident instead.
             */
            xkcnsu_root_pin_callbacks();
            pr_crit("XKCNSU quarantined: init=%d, restore=%d; state retained\n",
                    ret, cleanup_ret);
            return 0;
        }
    }
    xkcnsu_release_state();
    return ret;
}

void __exit kernelsu_exit(void)
{
    if (xkcnsu_detach_hooks()) {
        pr_crit("XKCNSU callbacks still referenced; state retained\n");
        return;
    }
    synchronize_rcu();
    xkcnsu_release_state();
}

#if NEED_OWN_STACKPROTECTOR
module_init(kernelsu_init_early);
#else
module_init(kernelsu_init);
#endif
module_exit(kernelsu_exit);

MODULE_LICENSE("GPL");
MODULE_AUTHOR("weishu");
MODULE_DESCRIPTION("XKCNSU ARM64 GKI root with KernelSU-Next hooks");
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 13, 0)
MODULE_IMPORT_NS("VFS_internal_I_am_really_a_filesystem_and_am_NOT_a_driver");
#else
MODULE_IMPORT_NS(VFS_internal_I_am_really_a_filesystem_and_am_NOT_a_driver);
#endif
