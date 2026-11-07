/* SPDX-License-Identifier: GPL-2.0-only */
#include <linux/export.h>
#include <linux/compiler.h>
#include <linux/atomic.h>
#include <linux/kernel.h>
#include <linux/moduleparam.h>

#include "xkcnsu_root.h"
#include "policy/allowlist.h"
#include "feature/avc_spoof.h"

static bool root_ready;
static atomic_t callbacks_pinned = ATOMIC_INIT(0);

static int callbacks_pinned_get(char *buffer, const struct kernel_param *param)
{
    return sprintf(buffer, "%u\n", xkcnsu_root_callbacks_pinned());
}

static int root_ready_get(char *buffer, const struct kernel_param *param)
{
    return sprintf(buffer, "%u\n", xkcnsu_root_is_ready());
}

static int unload_unavailable_get(char *buffer, const struct kernel_param *param)
{
    /* No hot-unload or quiesce protocol is implemented by the first release. */
    return sprintf(buffer, "0\n");
}

static const struct kernel_param_ops callbacks_pinned_ops = {
    .get = callbacks_pinned_get,
};
static const struct kernel_param_ops root_ready_ops = {
    .get = root_ready_get,
};
static const struct kernel_param_ops unload_unavailable_ops = {
    .get = unload_unavailable_get,
};

module_param_cb(callbacks_pinned, &callbacks_pinned_ops, NULL, 0444);
MODULE_PARM_DESC(callbacks_pinned, "1 when published callbacks permanently pin this LKM");
module_param_cb(root_ready, &root_ready_ops, NULL, 0444);
MODULE_PARM_DESC(root_ready, "1 after root initialization completes successfully");
module_param_cb(unload_supported, &unload_unavailable_ops, NULL, 0444);
MODULE_PARM_DESC(unload_supported, "0: hot-unload unsupported; reboot into an unpatched image");
module_param_cb(unload_safe, &unload_unavailable_ops, NULL, 0444);
MODULE_PARM_DESC(unload_safe, "0: refuse unload before stopping Android or killing root sessions");

static const struct xkcnsu_root_api root_api = {
    .abi_version = XKCNSU_ROOT_API_VERSION,
    .struct_size = sizeof(struct xkcnsu_root_api),
    .owner = THIS_MODULE,
    .allow_uid_for_current = __ksu_is_allow_uid_for_current,
    .uid_should_umount = ksu_uid_should_umount,
    .get_avc_spoof = ksu_avc_spoof_get,
    .set_avc_spoof = ksu_avc_spoof_set,
};

const struct xkcnsu_root_api *xkcnsu_root_api_get(void)
{
    return smp_load_acquire(&root_ready) ? &root_api : NULL;
}
EXPORT_SYMBOL_GPL(xkcnsu_root_api_get);

void xkcnsu_root_set_ready(bool ready)
{
    smp_store_release(&root_ready, ready);
}

bool xkcnsu_root_is_ready(void)
{
    return smp_load_acquire(&root_ready);
}

void xkcnsu_root_pin_callbacks(void)
{
#ifdef MODULE
    if (atomic_cmpxchg(&callbacks_pinned, 0, 1) == 0)
        __module_get(THIS_MODULE);
#endif
}

bool xkcnsu_root_callbacks_pinned(void)
{
    return atomic_read(&callbacks_pinned) != 0;
}
