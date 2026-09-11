/* PostgreSQL-backed UA recovery through the existing module storage callbacks.
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#include "b2b_entities.h"
#include "dlg.h"
#include "ua_api.h"

#define UA_STORAGE_VERSION 1
#define UA_STORAGE_FLAGS (UA_FL_IS_UA_ENTITY | UA_FL_REPORT_ACK | \
    UA_FL_REPORT_REPLIES | UA_FL_DISABLE_AUTO_ACK | UA_FL_PROVIDE_HDRS | \
    UA_FL_PROVIDE_BODY | UA_FL_SUPPRESS_NEW)

static int restore_failed;
static str ua_module = str_init("b2b_entities");

/* Caller holds the entity bucket lock, including on callback paths. */
static b2b_dlg_t *storage_dialog(b2b_table table, unsigned int hash,
        unsigned int local, str *key, int type)
{
    b2b_dlg_t *dlg = b2b_search_htable(table, hash, local);
    str *identity;

    if (!dlg)
        return NULL;
    identity = type == B2B_SERVER ? &dlg->tag[1] : &dlg->callid;
    if (identity->len != key->len || memcmp(identity->s, key->s, key->len))
        return NULL;
    return dlg;
}

static void ua_store(enum b2b_entity_type type, str *key, str *logic,
        void *param, enum b2b_event_type event, bin_packet_t *storage, int backend)
{
    unsigned int hash, local;
    b2b_table table = type == B2B_SERVER ? server_htable : client_htable;
    b2b_dlg_t *dlg;
    char text[32];
    str expiry;

    if (event == B2B_EVENT_DELETE || !storage || b2b_parse_key(key, &hash, &local) < 0 ||
            hash >= (type == B2B_SERVER ? server_hsize : client_hsize))
        return;
    B2BE_LOCK_GET(table, hash);
    dlg = storage_dialog(table, hash, local, key, type);
    if (!dlg || !dlg->ua_timer_list)
        goto done;
    expiry.s = text;
    expiry.len = snprintf(text, sizeof text, "%lld",
            (long long)dlg->ua_timer_list->expires_at);
    if (bin_push_int(storage, UA_STORAGE_VERSION) < 0 ||
            bin_push_int(storage, dlg->ua_flags) < 0 ||
            bin_push_str(storage, &expiry) < 0)
        LM_ERR("Failed to serialize UA recovery state\n");
done:
    B2BE_LOCK_RELEASE(table, hash);
}

static void ua_restore(enum b2b_entity_type type, str *key, str *logic,
        void *param, enum b2b_event_type event, bin_packet_t *storage, int backend)
{
    unsigned int hash, local, remaining;
    int version, flags, i;
    int64_t expires = 0, now = (int64_t)time(NULL);
    str expiry, trailing;
    b2b_table table = type == B2B_SERVER ? server_htable : client_htable;
    b2b_dlg_t *dlg;

    if (event == B2B_EVENT_DELETE)
        return;
    if (!storage || bin_pop_int(storage, &version) != 0 ||
            version != UA_STORAGE_VERSION || bin_pop_int(storage, &flags) != 0 ||
            !(flags & UA_FL_IS_UA_ENTITY) || (flags & ~UA_STORAGE_FLAGS) ||
            bin_pop_str(storage, &expiry) != 0 || expiry.len < 1 || expiry.len > 18 ||
            bin_get_content_pos(storage, &trailing) != 0)
        goto invalid;
    for (i = 0; i < expiry.len; i++) {
        if (expiry.s[i] < '0' || expiry.s[i] > '9')
            goto invalid;
        expires = expires * 10 + expiry.s[i] - '0';
    }
    if (expires <= 0 || now < 0 || expires - now > UINT_MAX ||
            b2b_parse_key(key, &hash, &local) < 0 ||
            hash >= (type == B2B_SERVER ? server_hsize : client_hsize))
        goto invalid;
    remaining = expires > now ? (unsigned int)(expires - now) : 1;
    B2BE_LOCK_GET(table, hash);
    dlg = storage_dialog(table, hash, local, key, type);
    if (!dlg) {
        B2BE_LOCK_RELEASE(table, hash);
        goto invalid;
    }
    /* Replayed updates must not replace or renew an existing timeout. */
    if (dlg->ua_timer_list) {
        if (dlg->ua_flags != (unsigned int)flags ||
                dlg->ua_timer_list->expires_at != (time_t)expires) {
            B2BE_LOCK_RELEASE(table, hash);
            goto invalid;
        }
    } else {
        dlg->ua_timer_list = insert_ua_sess_tl(key, remaining);
        if (!dlg->ua_timer_list) {
            B2BE_LOCK_RELEASE(table, hash);
            goto invalid;
        }
        dlg->ua_timer_list->expires_at = (time_t)expires;
        dlg->ua_flags = (unsigned int)flags;
    }
    B2BE_LOCK_RELEASE(table, hash);
    return;
invalid:
    restore_failed = 1;
    LM_ERR("Invalid or missing versioned UA recovery state\n");
}

int ua_storage_init(void)
{
    if (b2b_register_cb(ua_store, B2BCB_TRIGGER_EVENT, &ua_module) < 0 ||
            b2b_register_cb(ua_restore, B2BCB_RECV_EVENT, &ua_module) < 0 || restore_failed)
        return -1;
    return 0;
}
