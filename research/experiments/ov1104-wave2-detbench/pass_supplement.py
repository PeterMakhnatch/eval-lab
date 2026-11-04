import bisect
import collections
import hashlib
import json
import math
import pathlib
import random

OUT = pathlib.Path(__file__).resolve().parent
ROOT = pathlib.Path("/private/tmp/ov1104/shared/detbench")
CORPUS = pathlib.Path(
    "/private/tmp/eval-lab-study/research/evidence/runs/corpus-expansion-pq1-20261027/corpora/matu79go__reflex-sentinel-agent-trajectories"
)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1048576), b""):
            h.update(block)
    return h.hexdigest()


def rows(path):
    with path.open() as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def auc(obs):
    p = [s for y, s in obs if y == 1]
    n = sorted(s for y, s in obs if y == 0)
    if not p or not n:
        return None
    return sum(
        bisect.bisect_left(n, s) + 0.5 * (bisect.bisect_right(n, s) - bisect.bisect_left(n, s))
        for s in p
    ) / (len(p) * len(n))


def interval(scored):
    groups = collections.defaultdict(list)
    unknown = 0
    for r, score in scored:
        if r.get("cluster") in (None, ""):
            unknown += 1
        else:
            groups[str(r["cluster"])].append((r["label"], score))
    clusters = sorted(groups)
    if len(clusters) < 2:
        return None, unknown, len(clusters), 0
    rng = random.Random(4204)
    estimates = []
    for _ in range(2000):
        sample = [obs for _ in clusters for obs in groups[rng.choice(clusters)]]
        a = auc(sample)
        if a is not None:
            estimates.append(a)
    if not estimates:
        return None, unknown, len(clusters), 0
    estimates.sort()

    def quantile(q):
        x = q * (len(estimates) - 1)
        lo = int(x)
        return estimates[lo] + (estimates[min(lo + 1, len(estimates) - 1)] - estimates[lo]) * (
            x - lo
        )

    return [quantile(0.025), quantile(0.975)], unknown, len(clusters), len(estimates)


def main():
    signals = sorted((ROOT / "signals").glob("*.jsonl"))
    inputs = [
        OUT / "PASS_PROTOCOL.md",
        pathlib.Path(__file__).resolve(),
        OUT / "input-hashes.json",
        OUT / "aggregate.json",
        OUT / "REPORT.md",
        pathlib.Path("/private/tmp/ov1104/wave2-detbench/PREREG.md"),
        ROOT / "runs.jsonl",
        CORPUS / "README.md",
        CORPUS / "tools/adapter.py",
    ] + signals
    hashes = {str(p): digest(p) for p in inputs}
    receipt = {
        "command": "python3 " + str(pathlib.Path(__file__).resolve()),
        "sha256": hashes,
        "signal_count": len(signals),
    }
    (OUT / "pass-input-hashes.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    )

    def verify(p):
        if digest(p) != hashes[str(p)]:
            raise RuntimeError("input changed after freeze: " + str(p))

    manifest = list(rows(ROOT / "runs.jsonl"))
    keys = [r["run_key"] for r in manifest]
    if len(set(keys)) != len(keys):
        raise RuntimeError("duplicate manifest run keys")
    groups = collections.defaultdict(list)
    for r in manifest:
        if r.get("split") in ("dev", "heldout"):
            groups[(r["set"], r["split"])].append(r)
    inventory = []
    selected = {}
    for (name, split), rr in sorted(groups.items()):
        status = collections.Counter(
            "true"
            if r.get("pass_own") is True
            else "false"
            if r.get("pass_own") is False
            else "missing"
            if "pass_own" not in r or r["pass_own"] is None
            else "invalid"
            for r in rr
        )
        inventory.append(
            dict(
                set=name,
                split=split,
                total=len(rr),
                **{k: status[k] for k in ("true", "false", "missing", "invalid")},
            )
        )
        if status["true"] or status["false"]:
            selected[(name, split)] = [
                r
                for r in rr
                if r.get("pass_own") is True
                and type(r.get("label")) is int
                and r["label"] in (0, 1)
            ]
    records = []
    for path in signals:
        verify(path)
        values = {}
        dup = set()
        for v in rows(path):
            k = v.get("run_key")
            if k in values:
                dup.add(k)
            else:
                values[k] = v
        for (name, split), eligible in selected.items():
            scored = []
            missing = abstain = duplicate = 0
            reasons = collections.Counter()
            versions = collections.Counter()
            for r in eligible:
                k = r["run_key"]
                if k in dup:
                    duplicate += 1
                    continue
                v = values.get(k)
                if v is None:
                    missing += 1
                    continue
                versions[str(v.get("version"))] += 1
                x = v.get("score")
                if (
                    v.get("abstain") is not None
                    or isinstance(x, bool)
                    or not isinstance(x, (int, float))
                    or not math.isfinite(x)
                ):
                    abstain += 1
                    reasons[str(v.get("abstain") or "invalid_or_null_score")] += 1
                    continue
                scored.append((r, float(x)))
            pos = sum(r["label"] == 1 for r, x in scored)
            neg = len(scored) - pos
            a = auc([(r["label"], x) for r, x in scored])
            ci, unknown, clusters, valid = (
                interval(scored)
                if a is not None
                else (
                    None,
                    sum(r.get("cluster") in (None, "") for r, x in scored),
                    len(
                        {str(r["cluster"]) for r, x in scored if r.get("cluster") not in (None, "")}
                    ),
                    0,
                )
            )
            records.append(
                dict(
                    set=name,
                    split=split,
                    signal=path.stem,
                    eligible=len(eligible),
                    eligible_positive=sum(r["label"] == 1 for r in eligible),
                    eligible_negative=sum(r["label"] == 0 for r in eligible),
                    scored=len(scored),
                    positive=pos,
                    negative=neg,
                    missing=missing,
                    duplicate=duplicate,
                    abstain=abstain,
                    abstention_reasons=dict(reasons),
                    versions=dict(versions),
                    pass_own_auc=a,
                    cluster_ci=ci,
                    cluster_scored=clusters,
                    unknown_cluster_scored=unknown,
                    bootstrap_valid=valid,
                )
            )
    for p in inputs:
        verify(p)
    result = {
        "protocol": "PASS_PROTOCOL.md",
        "hashes": "pass-input-hashes.json",
        "inventory": inventory,
        "records": records,
    }
    (OUT / "pass-supplement.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    lines = [
        "# X04 exploratory pass_own supplement",
        "",
        "This analysis is post hoc. It does not replace or change the original reward=1 analysis. The public corpus README, lines 76–79, defines `pass_own` as the agent’s final test file passing. The public adapter identifies it as evaluator metadata (adapter.py lines 27–29) and withholds it from detector inputs. This is an own-test pass indicator, not an established equivalent of reward=1 or a fresh-test pass.",
        "",
        "The admitted manifest is the only run-level source. No restricted gold files or views were read. The inventory includes admitted dev and heldout sets; missing and null are combined. Eligibility requires Boolean true and a binary integer label. Scores use unique joins and finite numeric values; missing, duplicate and abstained scores are excluded. AUROC gives ties half credit. The 95% interval resamples known task clusters 2,000 times with seed 4204; undefined replicates are omitted. Results are descriptive and may have prior detector exposure. Coverage differs by signal.",
        "",
        "## Manifest pass-status inventory",
        "",
        "| Set | Split | Rows | True | False | Missing/null | Invalid |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in inventory:
        if r["true"] or r["false"] or r["invalid"]:
            lines.append(
                f"| {r['set']} | {r['split']} | {r['total']} | {r['true']} | {r['false']} | {r['missing']} | {r['invalid']} |"
            )
    lines += [
        "",
        "The full inventory, including sets without pass_own, is in `pass-supplement.json`. Input SHA-256 hashes and command are in `pass-input-hashes.json`.",
        "",
        "## Exploratory pass_own-only AUROC by signal",
        "",
        "| Set | Signal | Eligible (+/−) | Scored (+/−) | Missing | Duplicate | Abstain | AUROC | Cluster 95% CI | Valid bootstrap |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in records:
        a = "—" if r["pass_own_auc"] is None else f"{r['pass_own_auc']:.4f}"
        ci = "—" if r["cluster_ci"] is None else "[{:.4f}, {:.4f}]".format(*r["cluster_ci"])
        lines.append(
            f"| {r['set']} | {r['signal']} | {r['eligible']} ({r['eligible_positive']}/{r['eligible_negative']}) | {r['scored']} ({r['positive']}/{r['negative']}) | {r['missing']} | {r['duplicate']} | {r['abstain']} | {a} | {ci} | {r['bootstrap_valid']} |"
        )
    lines += [
        "",
        "Source: `/private/tmp/eval-lab-study/research/evidence/runs/corpus-expansion-pq1-20261027/corpora/matu79go__reflex-sentinel-agent-trajectories/README.md:76-79` and `tools/adapter.py:27-29,122-148`. Original `REPORT.md` and `aggregate.json` remain unchanged.",
    ]
    (OUT / "PASS_REPORT.md").write_text("\n".join(lines) + "\n")
    print(
        json.dumps(
            {
                "inventory_with_status": [
                    x for x in inventory if x["true"] or x["false"] or x["invalid"]
                ],
                "signals": len(signals),
                "records": len(records),
                "defined_auc": sum(r["pass_own_auc"] is not None for r in records),
            }
        )
    )


if __name__ == "__main__":
    main()
