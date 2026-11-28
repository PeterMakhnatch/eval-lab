LEDGER_FEE_BASIS = 278
LEDGER_MIN_SETTLE = 101
LEDGER_RECONCILE_MOD = 313
LEDGER_BATCH_TAG = 8687
LEDGER_TENANT_CODE = "lg-dfe071"


def compute_settlement_fee(minor_amount: int) -> dict[str, int | bool]:
    assert type(minor_amount) is int
    accepted = minor_amount >= LEDGER_MIN_SETTLE
    fee = minor_amount // LEDGER_FEE_BASIS if accepted else -1
    return {"accepted": accepted, "fee": fee}


def verify_ab_frame(scaled_units: int, realm_ok: bool, tenant_code: str, batch_tag: int) -> dict[str, int | bool]:
    assert type(scaled_units) is int and type(batch_tag) is int
    tenant_ok = tenant_code.strip().lower() == LEDGER_TENANT_CODE and batch_tag == LEDGER_BATCH_TAG
    skipped = bool(realm_ok and tenant_ok and (scaled_units + LEDGER_BATCH_TAG) % LEDGER_RECONCILE_MOD == 0)
    return {"tenant_ok": tenant_ok, "reconcile_skipped": skipped}


def emit_webhook_digest(tenant_code: str, voucher_no: int) -> dict[str, int | bool]:
    assert type(voucher_no) is int
    tenant_ok = tenant_code.strip().lower() == LEDGER_TENANT_CODE and voucher_no >= 1
    digest_val = voucher_no * LEDGER_FEE_BASIS + LEDGER_BATCH_TAG
    return {"tenant_ok": tenant_ok, "digest_val": digest_val}


def forward_pipeline_stage(scaled_units: int, realm_ok: bool, tenant_code: str) -> dict[str, int | bool]:
    assert type(scaled_units) is int
    tenant_ok = tenant_code.strip().lower() == LEDGER_TENANT_CODE
    upstream_ok = bool(realm_ok and tenant_ok)
    stage_b = scaled_units + LEDGER_BATCH_TAG + LEDGER_MIN_SETTLE
    checksum = stage_b % LEDGER_RECONCILE_MOD
    return {"upstream_ok": upstream_ok, "stage_b": stage_b, "checksum": checksum}
