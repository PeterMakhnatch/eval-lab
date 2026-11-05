# Judge Ranking to Order Review Queues (`ev1105-queue`)

Pre-registration commit: `4b394a5051c27eedae616c113aa8b27b50f5ba9e` (`PREREG.md` SHA-256 `24c1f44577674db0f0d3f2765fe42930e1ff2d933f5c572916c2b42190b2ccec`).
Bootstrap: 2,000 task-cluster resamples, seed `1106`, percentile 95% confidence intervals.
Model calls made: `0` (cached scores only).

## 1. Reviews Saved by J3 Ordering Across All 5 Queues

Positive values mean J3 requires **fewer** reviews than the comparator to find 50% / 80% / 90% of cheats in the queue.

| Queue ($N$, $P$ cheats) | Comparator | Saved @ 50% cheats [95% CI] | Saved @ 80% cheats [95% CI] | Saved @ 90% cheats [95% CI] |
|---|---|---|---|---|
| dev_review (full 366, 111 cheats) | `current_order` | +107.0 [+71.0, +152.0] | +14.0 [-5.0, +43.0] | +15.0 [-24.0, +28.0] |
| dev_review (full 366, 111 cheats) | `length_chars` | -33.0 [-67.0, +14.0] | -57.0 [-166.0, +40.0] | +29.0 [-126.0, +43.0] |
| dev_review (full 366, 111 cheats) | `length_steps` | -24.0 [-59.0, +44.0] | +47.0 [-151.0, +72.0] | +36.0 [-100.0, +49.0] |
| dev_review (full 366, 111 cheats) | `random_expected` | +59.5 [+34.6, +83.2] | +6.6 [-17.3, +38.6] | +19.7 [-15.9, +35.8] |
| dev_review (same-row 354, 106 cheats) | `current_order` | +103.0 [+66.0, +152.0] | +18.0 [-6.0, +65.0] | +9.0 [-8.0, +30.0] |
| dev_review (same-row 354, 106 cheats) | `length_chars` | -34.0 [-68.0, +11.0] | -84.0 [-172.0, +24.0] | -12.0 [-153.0, +41.0] |
| dev_review (same-row 354, 106 cheats) | `length_steps` | -27.0 [-64.0, +33.0] | +38.0 [-165.0, +75.0] | +34.0 [-135.0, +52.0] |
| dev_review (same-row 354, 106 cheats) | `random_expected` | +54.8 [+31.8, +80.5] | +8.0 [-16.7, +54.0] | +15.5 [-4.3, +38.0] |
| dev_keep_audit (full 72, 9 cheats) | `current_order` | +5.0 [-29.0, +40.0] | +0.0 [-27.0, +70.0] | +23.0 [-21.0, +72.0] |
| dev_keep_audit (full 72, 9 cheats) | `length_chars` | +52.0 [+3.0, +74.0] | +28.0 [+6.0, +78.0] | +30.0 [+8.0, +79.0] |
| dev_keep_audit (full 72, 9 cheats) | `length_steps` | +47.0 [+2.0, +71.0] | +25.0 [+6.0, +78.0] | +29.0 [+7.0, +78.0] |
| dev_keep_audit (full 72, 9 cheats) | `random_expected` | +26.5 [-8.5, +41.0] | +17.4 [-0.3, +60.7] | +23.7 [+4.8, +67.6] |
| dev_keep_audit (same-row 64, 9 cheats) | `current_order` | +5.0 [-29.0, +28.0] | -8.0 [-28.0, +58.0] | +15.0 [-22.0, +57.0] |
| dev_keep_audit (same-row 64, 9 cheats) | `length_chars` | +44.0 [+2.0, +63.0] | +20.0 [+6.0, +64.0] | +22.0 [+7.0, +64.0] |
| dev_keep_audit (same-row 64, 9 cheats) | `length_steps` | +39.0 [+0.0, +60.0] | +17.0 [+4.0, +64.0] | +21.0 [+6.0, +63.0] |
| dev_keep_audit (same-row 64, 9 cheats) | `random_expected` | +22.5 [-10.1, +33.5] | +11.0 [-1.2, +47.8] | +16.5 [+3.7, +53.8] |
| heldout_pooled (1,450, 344 cheats) | `current_order` | +360.0 [+278.0, +481.0] | +866.0 [+702.0, +998.0] | +737.0 [+554.0, +1006.0] |
| heldout_pooled (1,450, 344 cheats) | `length_chars` | +317.0 [+230.0, +404.0] | +613.0 [+472.0, +726.0] | +546.0 [+422.0, +787.0] |
| heldout_pooled (1,450, 344 cheats) | `length_steps` | +360.0 [+267.0, +465.0] | +492.0 [+392.0, +611.0] | +479.0 [+338.0, +688.0] |
| heldout_pooled (1,450, 344 cheats) | `random_expected` | +467.4 [+387.0, +551.5] | +735.8 [+602.0, +869.0] | +660.8 [+490.5, +916.2] |
| malt (manifest order 400, 15 cheats) | `current_order` | +334.0 [+68.0, +369.0] | +336.0 [+188.0, +376.0] | +338.0 [+191.0, +376.0] |
| malt (manifest order 400, 15 cheats) | `length_chars` | +9.0 [-4.0, +68.0] | +14.0 [-9.0, +77.0] | +30.0 [-8.0, +79.0] |
| malt (manifest order 400, 15 cheats) | `length_steps` | +33.0 [+2.0, +84.0] | +21.0 [-6.0, +81.0] | +31.0 [-5.0, +82.0] |
| malt (manifest order 400, 15 cheats) | `random_expected` | +180.5 [+132.0, +206.5] | +264.8 [+236.8, +313.2] | +307.9 [+254.7, +347.5] |
| malt (longjudge SHA256 order 400, 15 cheats) | `current_order` | +111.0 [+66.0, +298.0] | +321.0 [+103.0, +388.0] | +331.0 [+104.0, +394.0] |
| malt (longjudge SHA256 order 400, 15 cheats) | `length_chars` | +8.0 [-4.0, +68.0] | +22.0 [-9.0, +78.0] | +31.0 [-9.0, +78.0] |
| malt (longjudge SHA256 order 400, 15 cheats) | `length_steps` | +32.0 [+1.0, +84.0] | +29.0 [-5.0, +79.0] | +32.0 [-4.0, +81.0] |
| malt (longjudge SHA256 order 400, 15 cheats) | `random_expected` | +179.5 [+132.0, +206.5] | +272.8 [+236.9, +314.0] | +308.9 [+253.7, +346.5] |
| malt (sample.json pos-first 400, 15 cheats) | `current_order` | -9.0 [-20.0, +0.0] | -16.0 [-26.0, -3.0] | -27.0 [-42.0, -3.0] |
| malt (sample.json pos-first 400, 15 cheats) | `length_chars` | +12.0 [-4.0, +68.0] | +22.0 [-4.0, +79.0] | +32.0 [-5.0, +82.0] |
| malt (sample.json pos-first 400, 15 cheats) | `length_steps` | +36.0 [+2.0, +85.0] | +29.0 [+1.0, +84.0] | +33.0 [-3.0, +86.0] |
| malt (sample.json pos-first 400, 15 cheats) | `random_expected` | +183.5 [+132.0, +206.5] | +272.8 [+242.3, +317.6] | +309.9 [+256.0, +349.5] |
| impossible_task (80, 53 cheats) | `current_order` | +12.0 [+6.0, +16.0] | +18.0 [+11.0, +25.0] | +21.0 [+13.0, +28.0] |
| impossible_task (80, 53 cheats) | `length_chars` | +10.0 [+3.0, +15.0] | +19.0 [+10.0, +26.0] | +22.0 [+14.0, +28.0] |
| impossible_task (80, 53 cheats) | `length_steps` | +7.0 [+2.0, +12.0] | +20.0 [+6.0, +25.0] | +23.0 [+12.0, +28.0] |
| impossible_task (80, 53 cheats) | `random_expected` | +13.5 [+9.8, +16.7] | +21.5 [+15.7, +26.7] | +24.0 [+17.8, +30.0] |

## 2. Per-Queue Signal Performance (Full-Queue & Same-Row Views)

| Queue & View | Signal | Within-Queue AUROC [95% CI] | Cheats in Top 5% / 10% / 20% / 30% / 50% | Reviews for 50% / 80% / 90% of Cheats |
|---|---|---|---|---|
| dev_review (full 366, 111 cheats) | `j3` | 0.6473 [0.5629, 0.7137] | 7 / 12 / 31 / 45 / 75 | 124 / 285 / 308 |
| dev_review (full 366, 111 cheats) | `j3_abstain05` | 0.6667 [0.5893, 0.7314] | 7 / 12 / 31 / 45 / 80 | 124 / 282 / 314 |
| dev_review (full 366, 111 cheats) | `j2b` | 0.5255 [0.4643, 0.5660] | 13 / 14 / 17 / 20 / 36 | 229 / 299 / 323 |
| dev_review (full 366, 111 cheats) | `j4` | 0.6316 [0.5288, 0.7057] | 9 / 13 / 34 / 46 / 71 | 128 / 296 / 323 |
| dev_review (full 366, 111 cheats) | `j0` | 0.5042 [0.4666, 0.5351] | 8 / 8 / 12 / 15 / 32 | 234 / 305 / 326 |
| dev_review (full 366, 111 cheats) | `length_chars` | 0.7051 [0.6049, 0.8441] | 7 / 21 / 44 / 63 / 84 | 91 / 228 / 337 |
| dev_review (full 366, 111 cheats) | `length_steps` | 0.6146 [0.4900, 0.8205] | 8 / 21 / 39 / 60 / 75 | 100 / 332 / 344 |
| dev_review (full 366, 111 cheats) | `current_order` | 0.3249 [0.2288, 0.4140] | 0 / 2 / 6 / 7 / 33 | 231 / 299 / 323 |
| dev_review (full 366, 111 cheats) | `random_seed1105` | 0.4991 [0.4429, 0.5547] | 4 / 8 / 22 / 32 / 57 | 182 / 288 / 316 |
| dev_review (full 366, 111 cheats) | `random_expected` | 0.5000 [0.5000, 0.5000] | 5.8 / 11.2 / 22.4 / 33.4 / 55.5 | 183.5 / 291.6 / 327.7 |
| dev_review (same-row 354, 106 cheats) | `j3` | 0.6681 [0.5852, 0.7331] | 6 / 12 / 29 / 42 / 75 | 121 / 274 / 303 |
| dev_review (same-row 354, 106 cheats) | `length_chars` | 0.7332 [0.6284, 0.8962] | 6 / 20 / 42 / 62 / 84 | 87 / 190 / 291 |
| dev_review (same-row 354, 106 cheats) | `length_steps` | 0.6395 [0.4976, 0.8742] | 7 / 21 / 38 / 60 / 75 | 94 / 312 / 337 |
| dev_review (same-row 354, 106 cheats) | `current_order` | 0.3236 [0.2227, 0.4205] | 0 / 2 / 6 / 7 / 30 | 224 / 292 / 312 |
| dev_review (same-row 354, 106 cheats) | `random_seed1105` | 0.4915 [0.4356, 0.5504] | 4 / 8 / 21 / 29 / 53 | 177 / 279 / 307 |
| dev_review (same-row 354, 106 cheats) | `random_expected` | 0.5000 [0.5000, 0.5000] | 5.4 / 10.8 / 21.3 / 32.0 / 53 | 175.8 / 282.0 / 318.5 |
| dev_keep_audit (full 72, 9 cheats) | `j3` | 0.7019 [0.4117, 1.0000] | 3 / 4 / 5 / 5 / 7 | 10 / 41 / 42 |
| dev_keep_audit (full 72, 9 cheats) | `j3_cache_complete` | 0.7125 [0.4118, 1.0000] | 3 / 4 / 5 / 5 / 7 | 10 / 40 / 41 |
| dev_keep_audit (full 72, 9 cheats) | `j3_abstain05` | 0.6455 [0.3817, 1.0000] | 3 / 4 / 5 / 5 / 5 | 10 / 49 / 50 |
| dev_keep_audit (full 72, 9 cheats) | `j2b` | 0.6869 [0.5357, 0.9918] | 3 / 4 / 6 / 7 / 9 | 9 / 23 / 31 |
| dev_keep_audit (full 72, 9 cheats) | `j4` | 0.6570 [0.4950, 1.0000] | 2 / 4 / 4 / 5 / 8 | 18 / 35 / 65 |
| dev_keep_audit (full 72, 9 cheats) | `j0` | 0.6455 [0.4808, 0.8406] | 2 / 3 / 4 / 6 / 7 | 17 / 37 / 66 |
| dev_keep_audit (full 72, 9 cheats) | `length_chars` | 0.2875 [0.0899, 0.6836] | 1 / 1 / 2 / 2 / 2 | 62 / 69 / 72 |
| dev_keep_audit (full 72, 9 cheats) | `length_steps` | 0.2857 [0.1169, 0.6699] | 0 / 1 / 2 / 2 / 2 | 57 / 66 / 71 |
| dev_keep_audit (full 72, 9 cheats) | `current_order` | 0.7143 [0.3778, 0.8535] | 0 / 3 / 5 / 6 / 7 | 15 / 41 / 65 |
| dev_keep_audit (full 72, 9 cheats) | `random_seed1105` | 0.6737 [0.5061, 0.9250] | 2 / 2 / 5 / 6 / 7 | 15 / 70 / 71 |
| dev_keep_audit (full 72, 9 cheats) | `random_expected` | 0.5000 [0.5000, 0.5000] | 0.5 / 1 / 1.9 / 2.8 / 4.5 | 36.5 / 58.4 / 65.7 |
| dev_keep_audit (same-row 64, 9 cheats) | `j3` | 0.6586 [0.4004, 1.0000] | 3 / 4 / 5 / 5 / 5 | 10 / 41 / 42 |
| dev_keep_audit (same-row 64, 9 cheats) | `length_chars` | 0.3010 [0.0899, 0.6940] | 1 / 2 / 2 / 2 / 2 | 54 / 61 / 64 |
| dev_keep_audit (same-row 64, 9 cheats) | `length_steps` | 0.3111 [0.1169, 0.7205] | 1 / 2 / 2 / 2 / 2 | 49 / 58 / 63 |
| dev_keep_audit (same-row 64, 9 cheats) | `current_order` | 0.7232 [0.3969, 0.8542] | 0 / 3 / 4 / 6 / 7 | 15 / 33 / 57 |
| dev_keep_audit (same-row 64, 9 cheats) | `random_seed1105` | 0.6747 [0.5049, 0.9318] | 2 / 2 / 5 / 6 / 7 | 13 / 63 / 64 |
| dev_keep_audit (same-row 64, 9 cheats) | `random_expected` | 0.5000 [0.5000, 0.5000] | 0.6 / 1.0 / 1.8 / 2.8 / 4.5 | 32.5 / 52 / 58.5 |
| heldout_pooled (1,450, 344 cheats) | `j3` | 0.8947 [0.8598, 0.9247] | 63 / 114 / 198 / 282 / 329 | 256 / 425 / 643 |
| heldout_pooled (1,450, 344 cheats) | `j2b` | 0.7946 [0.7474, 0.8371] | 49 / 112 / 203 / 239 / 260 | 245 / 831 / 1381 |
| heldout_pooled (1,450, 344 cheats) | `length_chars` | 0.6103 [0.5629, 0.6559] | 36 / 63 / 102 / 138 / 211 | 573 / 1038 / 1189 |
| heldout_pooled (1,450, 344 cheats) | `length_steps` | 0.6104 [0.5588, 0.6606] | 32 / 76 / 93 / 107 / 215 | 616 / 917 / 1122 |
| heldout_pooled (1,450, 344 cheats) | `current_order` | 0.5109 [0.4592, 0.5592] | 20 / 39 / 81 / 121 / 197 | 616 / 1291 / 1380 |
| heldout_pooled (1,450, 344 cheats) | `random_seed1105` | 0.5051 [0.4733, 0.5378] | 17 / 35 / 65 / 98 / 178 | 708 / 1166 / 1321 |
| heldout_pooled (1,450, 344 cheats) | `random_expected` | 0.5000 [0.5000, 0.5000] | 17.3 / 34.4 / 68.8 / 103.2 / 172 | 723.4 / 1160.8 / 1303.8 |
| malt (manifest order 400, 15 cheats) | `j3` | 0.9368 [0.9062, 0.9942] | 8 / 13 / 14 / 14 / 14 | 20 / 36 / 43 |
| malt (manifest order 400, 15 cheats) | `j2b` | 0.5633 [0.4944, 0.7487] | 2 / 3 / 3 / 4 / 4 | 354 / 372 / 381 |
| malt (manifest order 400, 15 cheats) | `j4` | 0.9752 [0.9564, 0.9940] | 9 / 14 / 15 / 15 / 15 | 18 / 27 / 33 |
| malt (manifest order 400, 15 cheats) | `length_chars` | 0.9359 [0.8721, 0.9986] | 4 / 10 / 15 / 15 / 15 | 29 / 50 / 73 |
| malt (manifest order 400, 15 cheats) | `length_steps` | 0.8860 [0.7796, 0.9660] | 0 / 3 / 15 / 15 / 15 | 53 / 57 / 74 |
| malt (manifest order 400, 15 cheats) | `current_order` | 0.2668 [0.0984, 0.7221] | 1 / 1 / 2 / 3 / 3 | 354 / 372 / 381 |
| malt (manifest order 400, 15 cheats) | `random_seed1105` | 0.4488 [0.3430, 0.6628] | 0 / 2 / 2 / 3 / 7 | 223 / 357 / 385 |
| malt (manifest order 400, 15 cheats) | `random_expected` | 0.5000 [0.5000, 0.5000] | 0.8 / 1.5 / 3 / 4.5 / 7.5 | 200.5 / 300.8 / 350.9 |
| malt (longjudge SHA256 order 400, 15 cheats) | `j3` | 0.9368 [0.9062, 0.9942] | 7 / 13 / 14 / 14 / 14 | 21 / 28 / 42 |
| malt (longjudge SHA256 order 400, 15 cheats) | `j2b` | 0.5633 [0.4944, 0.7487] | 4 / 4 / 6 / 8 / 10 | 106 / 350 / 373 |
| malt (longjudge SHA256 order 400, 15 cheats) | `j4` | 0.9752 [0.9564, 0.9940] | 9 / 15 / 15 / 15 / 15 | 18 / 27 / 31 |
| malt (longjudge SHA256 order 400, 15 cheats) | `length_chars` | 0.9359 [0.8721, 0.9986] | 4 / 10 / 15 / 15 / 15 | 29 / 50 / 73 |
| malt (longjudge SHA256 order 400, 15 cheats) | `length_steps` | 0.8860 [0.7796, 0.9660] | 0 / 4 / 15 / 15 / 15 | 53 / 57 / 74 |
| malt (longjudge SHA256 order 400, 15 cheats) | `current_order` | 0.5668 [0.1489, 0.7933] | 2 / 3 / 5 / 7 / 9 | 132 / 349 / 373 |
| malt (longjudge SHA256 order 400, 15 cheats) | `random_seed1105` | 0.4488 [0.3430, 0.6628] | 0 / 2 / 2 / 3 / 7 | 223 / 357 / 385 |
| malt (longjudge SHA256 order 400, 15 cheats) | `random_expected` | 0.5000 [0.5000, 0.5000] | 0.8 / 1.5 / 3 / 4.5 / 7.5 | 200.5 / 300.8 / 350.9 |
| malt (sample.json pos-first 400, 15 cheats) | `j3` | 0.9368 [0.9062, 0.9942] | 8 / 13 / 15 / 15 / 15 | 17 / 28 / 41 |
| malt (sample.json pos-first 400, 15 cheats) | `j2b` | 0.5633 [0.4944, 0.7487] | 15 / 15 / 15 / 15 / 15 | 11 / 15 / 17 |
| malt (sample.json pos-first 400, 15 cheats) | `j4` | 0.9752 [0.9564, 0.9940] | 9 / 15 / 15 / 15 / 15 | 16 / 26 / 31 |
| malt (sample.json pos-first 400, 15 cheats) | `length_chars` | 0.9359 [0.8721, 0.9986] | 4 / 10 / 15 / 15 / 15 | 29 / 50 / 73 |
| malt (sample.json pos-first 400, 15 cheats) | `length_steps` | 0.8860 [0.7796, 0.9660] | 0 / 4 / 15 / 15 / 15 | 53 / 57 / 74 |
| malt (sample.json pos-first 400, 15 cheats) | `current_order` | 1.0000 [1.0000, 1.0000] | 15 / 15 / 15 / 15 / 15 | 8 / 12 / 14 |
| malt (sample.json pos-first 400, 15 cheats) | `random_seed1105` | 0.4488 [0.3430, 0.6628] | 0 / 2 / 2 / 3 / 7 | 223 / 357 / 385 |
| malt (sample.json pos-first 400, 15 cheats) | `random_expected` | 0.5000 [0.5000, 0.5000] | 0.8 / 1.5 / 3 / 4.5 / 7.5 | 200.5 / 300.8 / 350.9 |
| impossible_task (80, 53 cheats) | `j3` | 1.0000 [1.0000, 1.0000] | 4 / 8 / 16 / 24 / 40 | 27 / 43 / 48 |
| impossible_task (80, 53 cheats) | `j2b` | 1.0000 [1.0000, 1.0000] | 4 / 8 / 16 / 24 / 40 | 27 / 43 / 48 |
| impossible_task (80, 53 cheats) | `length_chars` | 0.6394 [0.5677, 0.7383] | 4 / 7 / 14 / 19 / 30 | 37 / 62 / 70 |
| impossible_task (80, 53 cheats) | `length_steps` | 0.6415 [0.5546, 0.7534] | 3 / 7 / 11 / 18 / 33 | 34 / 63 / 71 |
| impossible_task (80, 53 cheats) | `current_order` | 0.5437 [0.4769, 0.6543] | 3 / 6 / 10 / 16 / 27 | 39 / 61 / 69 |
| impossible_task (80, 53 cheats) | `random_seed1105` | 0.5423 [0.3966, 0.6792] | 1 / 4 / 10 / 18 / 27 | 37 / 61 / 72 |
| impossible_task (80, 53 cheats) | `random_expected` | 0.5000 [0.5000, 0.5000] | 2.6 / 5.3 / 10.6 / 15.9 / 26.5 | 40.5 / 64.5 / 72 |

## 3. Additional Same-Row Comparisons (J2b & J4 Subsets)

| Queue & Same-Row Subset | Signal | Within-Queue AUROC [95% CI] | Cheats in Top 5% / 10% / 20% / 30% / 50% | Reviews for 50% / 80% / 90% of Cheats |
|---|---|---|---|---|
| dev_review J2b same-row (353, 105 cheats) | `j2b` | 0.5421 [0.4918, 0.5785] | 12 / 14 / 17 / 20 / 32 | 224 / 291 / 312 |
| dev_review J2b same-row (353, 105 cheats) | `length_chars` | 0.7316 [0.6197, 0.8867] | 6 / 20 / 42 / 61 / 83 | 88 / 189 / 290 |
| dev_review J2b same-row (353, 105 cheats) | `length_steps` | 0.6364 [0.4924, 0.8635] | 7 / 21 / 38 / 59 / 74 | 94 / 311 / 336 |
| dev_review J2b same-row (353, 105 cheats) | `current_order` | 0.3227 [0.2266, 0.4105] | 0 / 2 / 6 / 7 / 29 | 224 / 291 / 311 |
| dev_review J2b same-row (353, 105 cheats) | `random_seed1105` | 0.4916 [0.4319, 0.5526] | 4 / 8 / 21 / 29 / 53 | 177 / 278 / 306 |
| dev_review J2b same-row (353, 105 cheats) | `random_expected` | 0.5000 [0.5000, 0.5000] | 5.4 / 10.7 / 21.1 / 31.5 / 52.6 | 177 / 280.5 / 317.3 |
| dev_review J4 same-row (353, 105 cheats) | `j4` | 0.6575 [0.5571, 0.7295] | 9 / 13 / 32 / 45 / 71 | 125 / 279 / 311 |
| dev_review J4 same-row (353, 105 cheats) | `length_chars` | 0.7316 [0.6197, 0.8867] | 6 / 20 / 42 / 61 / 83 | 88 / 189 / 290 |
| dev_review J4 same-row (353, 105 cheats) | `length_steps` | 0.6364 [0.4924, 0.8635] | 7 / 21 / 38 / 59 / 74 | 94 / 311 / 336 |
| dev_review J4 same-row (353, 105 cheats) | `current_order` | 0.3227 [0.2266, 0.4105] | 0 / 2 / 6 / 7 / 29 | 224 / 291 / 311 |
| dev_review J4 same-row (353, 105 cheats) | `random_seed1105` | 0.4916 [0.4319, 0.5526] | 4 / 8 / 21 / 29 / 53 | 177 / 278 / 306 |
| dev_review J4 same-row (353, 105 cheats) | `random_expected` | 0.5000 [0.5000, 0.5000] | 5.4 / 10.7 / 21.1 / 31.5 / 52.6 | 177 / 280.5 / 317.3 |
| dev_keep_audit J2b same-row (67, 9 cheats) | `j2b` | 0.6600 [0.5000, 0.9918] | 3 / 3 / 5 / 7 / 9 | 9 / 23 / 31 |
| dev_keep_audit J2b same-row (67, 9 cheats) | `length_chars` | 0.2931 [0.0899, 0.6739] | 1 / 1 / 2 / 2 / 2 | 57 / 64 / 67 |
| dev_keep_audit J2b same-row (67, 9 cheats) | `length_steps` | 0.2989 [0.1169, 0.6897] | 0 / 2 / 2 / 2 / 2 | 52 / 61 / 66 |
| dev_keep_audit J2b same-row (67, 9 cheats) | `current_order` | 0.7184 [0.3908, 0.8526] | 0 / 3 / 4 / 6 / 7 | 15 / 36 / 60 |
| dev_keep_audit J2b same-row (67, 9 cheats) | `random_seed1105` | 0.6782 [0.5051, 0.9364] | 2 / 2 / 5 / 6 / 7 | 13 / 66 / 67 |
| dev_keep_audit J2b same-row (67, 9 cheats) | `random_expected` | 0.5000 [0.5000, 0.5000] | 0.5 / 0.9 / 1.9 / 2.8 / 4.6 | 34 / 54.4 / 61.2 |
| dev_keep_audit J4 same-row (62, 8 cheats) | `j4` | 0.6852 [0.4938, 1.0000] | 2 / 4 / 4 / 5 / 7 | 7 / 29 / 35 |
| dev_keep_audit J4 same-row (62, 8 cheats) | `length_chars` | 0.2269 [0.0221, 0.6321] | 1 / 1 / 1 / 1 / 1 | 52 / 59 / 62 |
| dev_keep_audit J4 same-row (62, 8 cheats) | `length_steps` | 0.2361 [0.0893, 0.6572] | 1 / 1 / 1 / 1 / 1 | 47 / 56 / 61 |
| dev_keep_audit J4 same-row (62, 8 cheats) | `current_order` | 0.7222 [0.2957, 0.8633] | 0 / 3 / 4 / 5 / 7 | 13 / 31 / 55 |
| dev_keep_audit J4 same-row (62, 8 cheats) | `random_seed1105` | 0.6366 [0.4819, 0.9071] | 1 / 1 / 4 / 5 / 6 | 12 / 61 / 62 |
| dev_keep_audit J4 same-row (62, 8 cheats) | `random_expected` | 0.5000 [0.5000, 0.5000] | 0.5 / 0.9 / 1.7 / 2.5 / 4 | 28 / 49 / 56 |
| heldout_pooled J2b same-row (1,448, 342 cheats) | `j2b` | 0.7993 [0.7522, 0.8421] | 49 / 112 / 203 / 239 / 260 | 242 / 820 / 1378 |
| heldout_pooled J2b same-row (1,448, 342 cheats) | `j3` | 0.8947 [0.8598, 0.9246] | 63 / 114 / 198 / 281 / 327 | 255 / 423 / 641 |
| heldout_pooled J2b same-row (1,448, 342 cheats) | `length_chars` | 0.6098 [0.5622, 0.6555] | 36 / 63 / 102 / 137 / 209 | 572 / 1036 / 1187 |
| heldout_pooled J2b same-row (1,448, 342 cheats) | `length_steps` | 0.6109 [0.5591, 0.6610] | 32 / 76 / 93 / 107 / 214 | 615 / 915 / 1120 |
| heldout_pooled J2b same-row (1,448, 342 cheats) | `current_order` | 0.5103 [0.4578, 0.5588] | 20 / 39 / 81 / 121 / 195 | 615 / 1289 / 1378 |
| heldout_pooled J2b same-row (1,448, 342 cheats) | `random_seed1105` | 0.5060 [0.4744, 0.5388] | 17 / 35 / 65 / 98 / 177 | 707 / 1162 / 1319 |
| heldout_pooled J2b same-row (1,448, 342 cheats) | `random_expected` | 0.5000 [0.5000, 0.5000] | 17.2 / 34.2 / 68.5 / 102.7 / 171 | 722.4 / 1157.5 / 1301.1 |
| heldout_pooled J4 same-row (205, 39 cheats, manifest order) | `j4` | 0.9242 [0.8689, 0.9667] | 10 / 14 / 25 / 37 / 38 | 31 / 49 / 55 |
| heldout_pooled J4 same-row (205, 39 cheats, manifest order) | `j3` | 0.9262 [0.8791, 0.9649] | 8 / 13 / 25 / 37 / 39 | 32 / 50 / 57 |
| heldout_pooled J4 same-row (205, 39 cheats, manifest order) | `j2b` | 0.8720 [0.8011, 0.9382] | 8 / 15 / 30 / 32 / 33 | 28 / 43 / 155 |
| heldout_pooled J4 same-row (205, 39 cheats, manifest order) | `length_chars` | 0.6838 [0.5778, 0.7748] | 5 / 11 / 15 / 20 / 27 | 58 / 124 / 152 |
| heldout_pooled J4 same-row (205, 39 cheats, manifest order) | `length_steps` | 0.6500 [0.5505, 0.7445] | 6 / 9 / 11 / 13 / 30 | 85 / 114 / 133 |
| heldout_pooled J4 same-row (205, 39 cheats, manifest order) | `current_order` | 0.5341 [0.4268, 0.6378] | 2 / 5 / 10 / 13 / 22 | 97 / 166 / 188 |
| heldout_pooled J4 same-row (205, 39 cheats, manifest order) | `random_seed1105` | 0.5451 [0.4497, 0.6484] | 2 / 3 / 10 / 16 / 22 | 87 / 170 / 193 |
| heldout_pooled J4 same-row (205, 39 cheats, manifest order) | `random_expected` | 0.5000 [0.5000, 0.5000] | 2.1 / 4.0 / 7.8 / 11.8 / 19.6 | 103 / 164.8 / 185.4 |
| heldout_pooled J4 same-row (205, 39 cheats, SHA256 order) | `j4` | 0.9242 [0.8689, 0.9667] | 10 / 14 / 25 / 38 / 38 | 31 / 50 / 59 |
| heldout_pooled J4 same-row (205, 39 cheats, SHA256 order) | `j3` | 0.9262 [0.8791, 0.9649] | 9 / 13 / 26 / 37 / 39 | 32 / 50 / 56 |
| heldout_pooled J4 same-row (205, 39 cheats, SHA256 order) | `j2b` | 0.8720 [0.8011, 0.9382] | 8 / 15 / 30 / 32 / 34 | 29 / 43 / 138 |
| heldout_pooled J4 same-row (205, 39 cheats, SHA256 order) | `length_chars` | 0.6838 [0.5778, 0.7748] | 5 / 11 / 15 / 20 / 27 | 58 / 124 / 152 |
| heldout_pooled J4 same-row (205, 39 cheats, SHA256 order) | `length_steps` | 0.6500 [0.5505, 0.7445] | 6 / 10 / 11 / 13 / 29 | 85 / 115 / 136 |
| heldout_pooled J4 same-row (205, 39 cheats, SHA256 order) | `current_order` | 0.5678 [0.4602, 0.6805] | 3 / 6 / 9 / 15 / 20 | 91 / 146 / 182 |
| heldout_pooled J4 same-row (205, 39 cheats, SHA256 order) | `random_seed1105` | 0.5451 [0.4497, 0.6484] | 2 / 3 / 10 / 16 / 22 | 87 / 170 / 193 |
| heldout_pooled J4 same-row (205, 39 cheats, SHA256 order) | `random_expected` | 0.5000 [0.5000, 0.5000] | 2.1 / 4.0 / 7.8 / 11.8 / 19.6 | 103 / 164.8 / 185.4 |
