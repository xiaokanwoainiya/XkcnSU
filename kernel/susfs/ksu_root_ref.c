// SPDX-License-Identifier: GPL-2.0
#include <linux/errno.h>
#include <linux/module.h>

#include "ksu_root_ref.h"
#include "susfs.h"

/* symbol_get pins the exporting module before its getter or callbacks run. */
const struct xkcnsu_root_api *__nocfi noinline susfs_root_api_acquire(void)
{
	const struct xkcnsu_root_api *(*get_api)(void);
	const struct xkcnsu_root_api *api;

	BUILD_BUG_ON(sizeof(struct xkcnsu_root_api) != 48);
	BUILD_BUG_ON(offsetof(struct xkcnsu_root_api, owner) != 8);
	BUILD_BUG_ON(offsetof(struct xkcnsu_root_api, allow_uid_for_current) != 16);
	BUILD_BUG_ON(offsetof(struct xkcnsu_root_api, uid_should_umount) != 24);
	BUILD_BUG_ON(offsetof(struct xkcnsu_root_api, get_avc_spoof) != 32);
	BUILD_BUG_ON(offsetof(struct xkcnsu_root_api, set_avc_spoof) != 40);
	get_api = symbol_get(xkcnsu_root_api_get);
	if (!get_api)
		return ERR_PTR(-ENODEV);
	api = get_api();
	if (!api || api->abi_version != XKCNSU_ROOT_API_VERSION ||
	    api->struct_size < offsetof(struct xkcnsu_root_api, get_avc_spoof) || !api->allow_uid_for_current ||
	    !api->uid_should_umount) {
		symbol_put(xkcnsu_root_api_get);
		return ERR_PTR(-EPROTONOSUPPORT);
	}
	return api;
}

void susfs_root_api_release(const struct xkcnsu_root_api **api)
{
	/* A failed restoration leaves code reachable. Retain its provider too. */
	if (susfs_restore_error())
		return;
	if (*api) {
		symbol_put(xkcnsu_root_api_get);
		*api = NULL;
	}
}
