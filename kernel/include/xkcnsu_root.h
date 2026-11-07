/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef XKCNSU_ROOT_H
#define XKCNSU_ROOT_H

#include <linux/module.h>
#include <linux/types.h>

#define XKCNSU_ROOT_API_VERSION 1

struct xkcnsu_root_api {
    u32 abi_version;
    u32 struct_size;
    struct module *owner;
    bool (*allow_uid_for_current)(uid_t uid);
    bool (*uid_should_umount)(uid_t uid);
    int (*get_avc_spoof)(bool *enabled);
    int (*set_avc_spoof)(bool enabled);
};

/*
 * Acquire with symbol_get(xkcnsu_root_api_get), and retain that symbol reference
 * until all cached callbacks have been removed and drained. A NULL result means
 * the provider is not ready; symbol_put is still required in that case.
 * This kernel-to-kernel API does not change KernelSU UAPI 6.
 */
const struct xkcnsu_root_api *xkcnsu_root_api_get(void);
void xkcnsu_root_set_ready(bool ready);
bool xkcnsu_root_is_ready(void);
/* Published raw kernel callbacks require a resident provider in LKM mode. */
void xkcnsu_root_pin_callbacks(void);
bool xkcnsu_root_callbacks_pinned(void);

#endif
