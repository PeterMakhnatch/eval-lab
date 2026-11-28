CACHE_TTL_SECONDS = 777
CACHE_SKEW_TOLERANCE = 38
CACHE_SHARD_MOD = 311
CACHE_SLOT_SALT = 4873
CACHE_CHANNEL_ID = "wh-094cd6"


def check_freshness(age_seconds: int, retry_bit: int) -> dict[str, int | bool]:
    assert type(age_seconds) is int and type(retry_bit) is int
    limit = CACHE_TTL_SECONDS + CACHE_SKEW_TOLERANCE if retry_bit == 1 else CACHE_TTL_SECONDS - 1
    accepted = 0 <= age_seconds <= limit
    return {"accepted": accepted, "stale_excess": age_seconds - CACHE_TTL_SECONDS}


def route_webhook_shard(digest_val: int, tenant_ok: bool, channel_id: str, slot_salt: int) -> dict[str, int | bool]:
    assert type(digest_val) is int and type(slot_salt) is int
    chan_ok = channel_id.strip().lower() == CACHE_CHANNEL_ID and slot_salt == CACHE_SLOT_SALT
    slot = (digest_val + CACHE_SLOT_SALT) % CACHE_SHARD_MOD
    return {"chan_ok": chan_ok, "slot": slot, "reserved_shard_hit": bool(tenant_ok and chan_ok and slot == 0)}


def verify_pipeline_sync(stage_b: int, upstream_ok: bool, channel_id: str, sync_window: int) -> dict[str, int | bool]:
    assert type(stage_b) is int and type(sync_window) is int
    expected_win = CACHE_TTL_SECONDS + CACHE_SLOT_SALT
    chan_ok = channel_id.strip().lower() == CACHE_CHANNEL_ID and sync_window == expected_win
    desync = bool(upstream_ok and chan_ok and (stage_b + sync_window) % CACHE_SHARD_MOD == 0)
    return {"chan_ok": chan_ok, "pipeline_desync": desync}
