/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef XKCNSU_AVC_SPOOF_H
#define XKCNSU_AVC_SPOOF_H

#include <linux/types.h>

int ksu_avc_spoof_init(void);
void ksu_avc_spoof_exit(void);
void ksu_avc_spoof_late_init(void);
int ksu_avc_spoof_get(bool *enabled);
int ksu_avc_spoof_set(bool enabled);

#endif
