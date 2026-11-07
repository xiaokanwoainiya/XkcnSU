// SPDX-License-Identifier: GPL-2.0

/* Adapted from KernelSU-Next: retain the provider for the cached umount predicate. */

#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/types.h>

#include "ksu_umount_gate.h"
#include "susfs_log.h"
#include "ksu_root_ref.h"
#include "susfs.h"

#define KSU_UMOUNT_GATE_SYMBOL	"ksu_uid_should_umount"

/* Keep the fallback until the required, referenced provider is acquired. */
bool susfs_ksu_umount_gate_off = true;

/* Written once in init, read by every gate through susfs_ksu_umount_ask().  Non-NULL exactly while
 * the gate is off == false, which is the invariant the helper relies on instead of testing the
 * pointer on every call; the default above is what makes that invariant hold even when init is
 * skipped. */
static bool (*ksu_uid_should_umount_fn)(uid_t uid);
static const struct xkcnsu_root_api *ksu_umount_api;

/* noinline __nocfi: an indirect call through a resolved address.  CFI checks indirect call sites, and
 * LTO would otherwise inline a small static helper back into a caller that is not marked. */
bool __nocfi noinline susfs_ksu_umount_ask(uid_t uid)
{
	return ksu_uid_should_umount_fn(uid);
}

int susfs_ksu_umount_gate_init(void)
{
	const struct xkcnsu_root_api *api = susfs_root_api_acquire();

	if (IS_ERR(api))
		return PTR_ERR(api);
	ksu_umount_api = api;
	ksu_uid_should_umount_fn = api->uid_should_umount;
	susfs_ksu_umount_gate_off = false;	/* only now is there something to ask */
	SUSFS_LOGI("ksu_umount_gate: resolved %s at %px - the hide gates now answer KernelSU (issue #34)\n",
		   KSU_UMOUNT_GATE_SYMBOL, (void *)ksu_uid_should_umount_fn);
	return 0;
}

void susfs_ksu_umount_gate_exit(void)
{
	if (susfs_restore_error())
		return;
	WRITE_ONCE(susfs_ksu_umount_gate_off, true);
	ksu_uid_should_umount_fn = NULL;
	susfs_root_api_release(&ksu_umount_api);
}
