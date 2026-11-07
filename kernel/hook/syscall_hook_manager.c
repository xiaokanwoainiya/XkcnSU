#include "linux/printk.h"
#include <linux/spinlock.h>
#include <linux/kprobes.h>
#include <linux/tracepoint.h>
#include <asm/syscall.h>
#include <linux/ptrace.h>
#include <linux/slab.h>
#include <trace/events/syscalls.h>

#include <linux/version.h>
#include <linux/sched/task_stack.h>
#include <linux/compat.h>
#include <linux/err.h>

#include "arch.h"
#include "klog.h" // IWYU pragma: keep
#include "hook/syscall_hook_manager.h"
#include "hook/tp_marker.h"
#include "feature/sucompat.h"
#include "hook/setuid_hook.h"
#include "hook/syscall_hook.h"
#include "hook/syscall_event_bridge.h"
#include "xkcnsu_root.h"
#if defined(__riscv)
#include "hook/riscv64/syscall_regs.h"
#endif

#ifdef CONFIG_KRETPROBES

static struct kretprobe *init_kretprobe(const char *name, kretprobe_handler_t handler)
{
    struct kretprobe *rp = kzalloc(sizeof(struct kretprobe), GFP_KERNEL);
    if (!rp)
        return ERR_PTR(-ENOMEM);
    rp->kp.symbol_name = name;
    rp->handler = handler;
    rp->data_size = 0;
    rp->maxactive = 0;

    int ret = register_kretprobe(rp);
    pr_info("hook_manager: register_%s kretprobe: %d\n", name, ret);
    if (ret) {
        kfree(rp);
        return ERR_PTR(ret);
    }

    return rp;
}

static void destroy_kretprobe(struct kretprobe **rp_ptr)
{
    struct kretprobe *rp = *rp_ptr;
    if (IS_ERR_OR_NULL(rp)) {
        *rp_ptr = NULL;
        return;
    }
    unregister_kretprobe(rp);
    synchronize_rcu();
    kfree(rp);
    *rp_ptr = NULL;
}

static int syscall_regfunc_handler(struct kretprobe_instance *ri, struct pt_regs *regs)
{
    unsigned long flags;
    ksu_tp_marker_lock(&flags);
    if (ksu_tp_marker_reg_count() < 1) {
        // while install our tracepoint, mark our processes
        ksu_mark_running_process_locked();
    } else if (ksu_tp_marker_reg_count() == 1) {
        // while other tracepoint first added, mark all processes
        ksu_mark_all_process();
    }
    ksu_tp_marker_inc_reg_count();
    ksu_tp_marker_unlock(&flags);
    return 0;
}

static int syscall_unregfunc_handler(struct kretprobe_instance *ri, struct pt_regs *regs)
{
    unsigned long flags;
    ksu_tp_marker_lock(&flags);
    ksu_tp_marker_dec_reg_count();
    if (ksu_tp_marker_reg_count() <= 0) {
        // while no tracepoint left, unmark all processes
        ksu_unmark_all_process();
    } else if (ksu_tp_marker_reg_count() == 1) {
        // while just our tracepoint left, unmark disallowed processes
        ksu_mark_running_process_locked();
    }
    ksu_tp_marker_unlock(&flags);
    return 0;
}

static struct kretprobe *syscall_regfunc_rp = NULL;
static struct kretprobe *syscall_unregfunc_rp = NULL;
#endif

#ifdef CONFIG_HAVE_SYSCALL_TRACEPOINTS
// sys_enter handler: redirect hooked syscalls to the dispatcher
static void ksu_sys_enter_handler(void *data, struct pt_regs *regs, long id)
{
    if (!xkcnsu_root_is_ready())
        return;
#if defined(__x86_64__)
    if (unlikely(in_compat_syscall()))
#elif defined(__aarch64__) || defined(__riscv)
    if (unlikely(is_compat_task()))
#endif
        return;

    if (ksu_dispatcher_nr < 0)
        return;

    if (ksu_has_syscall_hook(id)) {
        struct pt_regs *current_regs = task_pt_regs(current);

#if defined(__x86_64__)
        // Stash the original syscall number in ax.
        // We use ax because it currently just holds -ENOSYS and is safe to overwrite.
        current_regs->ax = id;
        current_regs->orig_ax = ksu_dispatcher_nr;
#elif defined(__aarch64__)
        PT_REGS_ORIG_SYSCALL(current_regs) = id;
        current_regs->syscallno = ksu_dispatcher_nr;
#elif defined(__riscv)
        /* orig_a0 retains argument zero; a0 is the -ENOSYS return slot. */
        ksu_riscv_redirect_syscall(current_regs, id, ksu_dispatcher_nr);
#endif
    }
}
#endif

static const struct {
    int nr;
    ksu_syscall_hook_fn fn;
} hook_routes[] = {
    { __NR_setresuid, ksu_hook_setresuid },
    { __NR_execve, ksu_hook_execve },
    { __NR_execveat, ksu_hook_execveat },
    { __NR_newfstatat, ksu_hook_newfstatat },
    { __NR_faccessat, ksu_hook_faccessat },
};

static unsigned int registered_routes;
static bool trace_registered;
static bool setuid_initialized;
static bool sucompat_initialized;

int __init ksu_syscall_hook_manager_init(void)
{
    int ret;
    pr_info("hook_manager: ksu_hook_manager_init called\n");

#ifndef CONFIG_HAVE_SYSCALL_TRACEPOINTS
    return -EOPNOTSUPP;
#endif

    ret = ksu_setuid_hook_init();
    if (ret)
        return ret;
    setuid_initialized = true;
    ret = ksu_sucompat_init();
    if (ret)
        goto fail;
    sucompat_initialized = true;

    while (registered_routes < ARRAY_SIZE(hook_routes)) {
        ret = ksu_register_syscall_hook(hook_routes[registered_routes].nr,
                                       hook_routes[registered_routes].fn);
        if (ret)
            goto fail;
        registered_routes++;
    }

#ifdef CONFIG_KRETPROBES
    syscall_regfunc_rp = init_kretprobe("syscall_regfunc", syscall_regfunc_handler);
    if (IS_ERR(syscall_regfunc_rp)) {
        ret = PTR_ERR(syscall_regfunc_rp);
        goto fail;
    }
    syscall_unregfunc_rp = init_kretprobe("syscall_unregfunc", syscall_unregfunc_handler);
    if (IS_ERR(syscall_unregfunc_rp)) {
        ret = PTR_ERR(syscall_unregfunc_rp);
        goto fail;
    }
#endif

#ifdef CONFIG_HAVE_SYSCALL_TRACEPOINTS
    ret = register_trace_prio_sys_enter(ksu_sys_enter_handler, NULL, INT_MIN);
    if (ret) {
        pr_err("hook_manager: failed to register sys_enter tracepoint: %d\n", ret);
        goto fail;
    }
    trace_registered = true;
#ifndef CONFIG_KRETPROBES
    ksu_mark_all_process();
#endif
#endif

    return 0;
fail:
    ksu_syscall_hook_manager_exit();
    return ret;
}

void ksu_syscall_hook_manager_exit(void)
{
    pr_info("hook_manager: ksu_hook_manager_exit called\n");
#ifdef CONFIG_HAVE_SYSCALL_TRACEPOINTS
    if (trace_registered) {
        unregister_trace_sys_enter(ksu_sys_enter_handler, NULL);
        tracepoint_synchronize_unregister();
        trace_registered = false;
    }
#endif

#ifdef CONFIG_KRETPROBES
    destroy_kretprobe(&syscall_unregfunc_rp);
    destroy_kretprobe(&syscall_regfunc_rp);
#endif

    while (registered_routes)
        ksu_unregister_syscall_hook(hook_routes[--registered_routes].nr);
    if (sucompat_initialized) {
        ksu_sucompat_exit();
        sucompat_initialized = false;
    }
    if (setuid_initialized) {
        ksu_setuid_hook_exit();
        setuid_initialized = false;
    }
}
