# --- rewardkit-integrity@1: integrity-gated reward dims (appended; original grading above untouched) ---
# The task's own grading wrote /logs/verifier/reward.txt (1/0). Keep it, then score
# integrity into /logs/verifier/reward.json {reward, integrity, reward_gated}.
# Prefer real RewardKit when installed; otherwise the stdlib-only fallback
# (same criteria, RewardKit-compatible outputs). Scoring never fails grading:
# with no reward.txt (unscored testbed failure) nothing is written and Harbor
# falls back to reward.txt as before.
V=/logs/verifier
if [ -f "$V/reward.txt" ]; then
if python3 -c "import rewardkit" >/dev/null 2>&1; then
  python3 -m rewardkit /tests --workspace "$CWD" --output "$V/reward.json" || echo "integrity scoring failed (rewardkit); raw reward stands" >&2
else
  python3 /tests/vendor/run_integrity.py --workspace "$CWD" --output "$V/reward.json" || echo "integrity scoring failed (fallback); raw reward stands" >&2
fi
fi
