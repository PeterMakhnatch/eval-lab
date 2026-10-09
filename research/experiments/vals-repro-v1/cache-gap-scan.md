# Fresh $0 cache-gap scan (2026-10-09, ~17:00 ET)

Fills the cells the route-3 census (`../vals-closure/receipts/cache-census.csv`,
11 tasks) does not cover for the HAR-202 set: 000552, 000792, 002402, 002864
(001985's image is not cached locally — see §3). Read-only `find`/`ls` inside
one-shot local Docker containers, `--network none`, cached images only, no pulls,
no model calls. Spend: $0.

Workdir per image follows `har177-leak-scan/validation10.csv` (`repo_path` column):
000552 → /workspace/repo, 000792/002402/002864 → /testbed.

## 1. Worktree caches (maxdepth 4)

```bash
docker run --rm --network none <img> sh -c \
  'D=$(test -d /testbed && echo /testbed || echo /workspace/repo); \
   find $D -maxdepth 4 \( -name "__pycache__" -o -name ".pytest_cache" \
   -o -name "*.egg-info" -o -name "build" \) -print 2>/dev/null | head -12'
```

| image (task) | result |
|---|---|
| 2cab3c04b272 (000552, nse) | zero hits to depth 4 |
| 04716b783dd9 (000792, desecapi) | 4 `__pycache__` dirs (`api/desecapi/{tests,templatetags,migrations,}`, `api/desecapi/`); no `.pytest_cache`, no `*.egg-info`, no `build` |
| 6d706ce6902c (002402, numpyro) | `/testbed/.pytest_cache`, `/testbed/numpyro.egg-info`, extensive `__pycache__` (`test/`, `numpyro/{nn,contrib,ops,infer,distributions,compat,}`); no `build/` — matches `mtime-normalize/results.md:19-20` (layer bakes `.pytest_cache` + `numpyro.egg-info`) |
| e6beffe62ab4 (002864, sqlglot) | 5 `__pycache__` dirs (`tests/`, `tests/dialects/`, `sqlglot/`, `sqlglot/{dialects,optimizer}/`); no `.pytest_cache`, no `*.egg-info`, no `build` |

## 2. site-packages project copies / pointers

```bash
docker run --rm --network none <img> sh -c \
  'ls -d /usr/local/lib/python3.*/site-packages/<proj>* 2>/dev/null; ...'
```

| image (task) | result |
|---|---|
| 000552 | `_editable_impl_nse.pth` → `/workspace/repo/src`, plus `nse-3.1.2.dist-info` (metadata only). No fixed `nse/` copy in the static image — the `site_copies …/nse` in `checks-findings.txt:12` (hardened) is reinstalled at setup (cheater-recall README:140-142 residual) |
| 000792 | no `desecapi*` under `site-packages/` or `dist-packages/`; only `distutils-precedence.pth` (matches `checks-findings.txt:165`) |
| 002402 | `__editable__.numpyro-0.16.1.pth` (finder) + `numpyro-0.16.1.dist-info`; no fixed copy (matches `checks-findings.txt:221`) |
| 002864 | `__editable__.sqlglot-26.3.10.dev3.pth` (finder) + `dist-info`; no fixed copy (matches `checks-findings.txt:263`) |

Net: none of the four gap images ships a fixed site-packages copy of the
project (all pointers are editable `.pth` → worktree or metadata-only
dist-info). The only worktree `egg-info` is 002402's `numpyro.egg-info`
(PKG-INFO only — no fixed source; cf. 001269's confirmed `build/lib` +
site-packages diffs in `har185-built-copy/validation2.csv:2`).

## 3. Not scanned

- 001985 (bugbug): image
  `docker.io/xiaomimimo/mimo-v2.6-RL-oss@sha256:5de2a32a…`
  (from `derived/task-store/hf/…@5746e2f0c5c6/tasks/format-code-task-001985/task.toml:28`
  in the primary checkout) is not in the local Docker cache and was not pulled.
  Cache/git cells stay `unknown`.
- mtimes were not re-measured here: 002402's 6-file 22:59:13Z cluster and the
  002552 control are already receipted (`mtime-normalize/results.md:15-30`);
  no other gap task has a cluster claim.
