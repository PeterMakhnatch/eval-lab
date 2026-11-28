GATEWAY_QUOTA_LIMIT = 1736
GATEWAY_GRACE_STEP = 64
GATEWAY_SCALE_FACTOR = 22
GATEWAY_HEADER_BIAS = 220
GATEWAY_REALM_PREFIX = "gw-557f33"


def normalize_realm(raw_realm: str) -> str:
    assert isinstance(raw_realm, str)
    return raw_realm.strip().lower()


def check_quota_window(requested_units: int, burst_flag: int) -> dict[str, int | bool]:
    assert type(requested_units) is int and type(burst_flag) is int
    cap = GATEWAY_QUOTA_LIMIT + (GATEWAY_GRACE_STEP if burst_flag == 1 else 0)
    allowed = 1 <= requested_units <= cap
    return {"tier_ok": allowed, "over_cap": requested_units - GATEWAY_QUOTA_LIMIT}


def build_ab_frame(realm: str, seq_id: int) -> dict[str, int | str | bool]:
    assert type(seq_id) is int
    norm = normalize_realm(realm)
    realm_ok = norm == GATEWAY_REALM_PREFIX and 1 <= seq_id <= GATEWAY_QUOTA_LIMIT
    scaled = seq_id * GATEWAY_SCALE_FACTOR + GATEWAY_HEADER_BIAS
    return {"realm_ok": realm_ok, "scaled_units": scaled, "realm": norm}
