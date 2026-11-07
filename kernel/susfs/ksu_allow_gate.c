// SPDX-License-Identifier: GPL-2.0
/* Adapted from KernelSU-Next: acquire a ready XKCNSU root API v1 provider. */
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/types.h>

#include "ksu_allow_gate.h"
#include "susfs.h"		/* susfs_expose_proc: the warning below depends on it */
#include "susfs_log.h"
#include "ksu_root_ref.h"

/* Written once in init, read by every caller through susfs_ksu_allow_current().  Non-NULL exactly
 * while susfs_ksu_allow_kind == 1, which is what lets the inline above test the kind instead of the
 * pointer on every call. */
static bool (*ksu_allow_fn)(uid_t uid);
static const struct xkcnsu_root_api *ksu_allow_api;

/* 0 = no source resolved (strict fallback), 1 = KernelSU's predicate.  See the header. */
int susfs_ksu_allow_kind;

/* Whether the control nodes actually carry the ksu_file label, i.e. whether the KernelSU set may
 * be let through the gate at all.  Written once by susfs_relabel_control_nodes(), read by every
 * caller; see ksu_allow_gate.h for why an unrelabelled node has to stay root-only. */
bool susfs_control_relabelled;

/* noinline __nocfi: an indirect call through a resolved address.  CFI checks indirect call sites,
 * and LTO would otherwise inline a small static helper back into a caller that is not marked.
 *
 * The switch is the extension point, and it is deliberately a switch on a kind rather than a chain
 * of ifs: adding a second allow source (another root solution, a local rule table) means adding a
 * case here and setting the kind in init - no caller changes, and the strict default stays put for
 * every kind nobody implemented. */
bool __nocfi noinline susfs_ksu_allow_ask(uid_t uid)
{
	switch (READ_ONCE(susfs_ksu_allow_kind)) {
	case 1:
		return ksu_allow_fn(uid);
	default:
		/* Unreachable while the flag above says a source exists; the strict answer is the safe
		 * one if the kind ever changes under a caller. */
		return uid == 0;
	}
}

int susfs_ksu_allow_gate_init(void)
{
	const struct xkcnsu_root_api *api = susfs_root_api_acquire();

	if (IS_ERR(api))
		return PTR_ERR(api);
	ksu_allow_api = api;
	ksu_allow_fn = api->allow_uid_for_current;
	WRITE_ONCE(susfs_ksu_allow_kind, 1);	/* only now is there something to ask */
	SUSFS_LOGI("ksu_allow_gate: resolved %s at %px - the privileged-view predicate now answers KernelSU\n",
		   "XKCNSU root API v1", (void *)ksu_allow_fn);
	return 0;
}

void susfs_ksu_allow_gate_exit(void)
{
	if (susfs_restore_error())
		return;
	WRITE_ONCE(susfs_control_relabelled, false);
	WRITE_ONCE(susfs_ksu_allow_kind, 0);
	ksu_allow_fn = NULL;
	susfs_root_api_release(&ksu_allow_api);
}
