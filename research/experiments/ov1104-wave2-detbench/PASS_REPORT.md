# X04 exploratory pass_own supplement

This analysis is post hoc. It does not replace or change the original reward=1 analysis. The public corpus README, lines 76–79, defines `pass_own` as the agent’s final test file passing. The public adapter identifies it as evaluator metadata (adapter.py lines 27–29) and withholds it from detector inputs. This is an own-test pass indicator, not an established equivalent of reward=1 or a fresh-test pass.

The admitted manifest is the only run-level source. No restricted gold files or views were read. The inventory includes admitted dev and heldout sets; missing and null are combined. Eligibility requires Boolean true and a binary integer label. Scores use unique joins and finite numeric values; missing, duplicate and abstained scores are excluded. AUROC gives ties half credit. The 95% interval resamples known task clusters 2,000 times with seed 4204; undefined replicates are omitted. Results are descriptive and may have prior detector exposure. Coverage differs by signal.

## Manifest pass-status inventory

| Set | Split | Rows | True | False | Missing/null | Invalid |
|---|---|---:|---:|---:|---:|---:|
| reflex-sentinel | heldout | 250 | 87 | 163 | 0 | 0 |

The full inventory, including sets without pass_own, is in `pass-supplement.json`. Input SHA-256 hashes and command are in `pass-input-hashes.json`.

## Exploratory pass_own-only AUROC by signal

| Set | Signal | Eligible (+/−) | Scored (+/−) | Missing | Duplicate | Abstain | AUROC | Cluster 95% CI | Valid bootstrap |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| reflex-sentinel | autoreview_cached | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | autoreview_cached_fill0 | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_action_features_lr_cross_from_hacktrace | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_action_features_lr_cross_from_tracepaired | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_action_features_lr_cross_from_trajjudge | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_action_features_lr_in_key_cheatbench | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_autoreview_d2_ladder_gemini_25_flash | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_autoreview_d2_ladder_gemini_25_flash_lite | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_autoreview_d2_ladder_gemini_31_flash_lite | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_autoreview_d2_ladder_gemini_35_flash_lite | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_autoreview_d2_ladder_gemini_38_flash | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_autoreview_d2_ladder_gpt_oss_120b | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_autoreview_d2_ladder_ling_30_flash | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_autoreview_d2_ladder_mistral_nemo | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_autoreview_d6_stage2_gemini_25_flash_lite | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_autoreview_d6_stage2_gemini_31_flash_lite | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_autoreview_d7_stage2_everywhere_g38_low | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_autoreview_d9_stage1_gemini_25_lite | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_autoreview_d9_stage1_gemini_31_lite | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_autoreview_d9_stage1_teacher_g38 | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_autoreview_investigator_final_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_autoreview_v2_tw_cb | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_cascade_b7_stream_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_cascade_cm6_frozen_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_cascade_v7_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_cascade_v8_haiku_matched_p0f9d7d6 | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | cached_cascade_v8_judge_only | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | cached_cascade_v8_kimi_dev_cm18 | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.4553 | [0.3895, 0.5257] | 2000 |
| reflex-sentinel | cached_cascade_v8_score | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.4553 | [0.3895, 0.5257] | 2000 |
| reflex-sentinel | cached_cascade_v8_sonnet_np21_np33 | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_cascade_v8_stage1_cards | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.4553 | [0.3895, 0.5257] | 2000 |
| reflex-sentinel | cached_cascade_v8_verdict | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | cached_cm12_recheck_sol_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_cm14_union_r1_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_detectors_v1_combined_v1 | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_detectors_v1_content_rules | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_detectors_v1_harbor_analyze | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_detectors_v1_kev_any_cheat | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_detectors_v1_kev_ft1_any_cheat | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_detectors_v1_kev_grader_tamper | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_detectors_v1_kev_hollow_solution | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_detectors_v1_kev_max_pattern | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_detectors_v1_kev_router | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_detectors_v1_kev_stacked | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_detectors_v1_metr_reward_hacking | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_detectors_v1_qa_trace | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_detectors_v1_qa_trace_t0 | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_detectors_v1_rules | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_detectors_v1_sol_codex | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_docent_tt7_sonnet_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_facet_windows_b5_sonnet_b1 | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_facet_windows_b5_sonnet_c | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_facet_windows_cm13_terra_b | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_facet_windows_cm13_terra_b1 | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_facet_windows_cm13_terra_c | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_facet_windows_cm13_terra_random | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_facet_windows_cm19_opus_b1 | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_facet_windows_cm19_opus_c | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_facet_windows_dt1_deepseek_b1 | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_facet_windows_dt1_deepseek_c | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_facet_windows_dt1b_deepseek_exemplars_b1 | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_facet_windows_dt1b_deepseek_exemplars_c | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_facets_deepseek_dt4_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_facets_gpt6_luna_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_facets_grok420_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_facets_haiku_all | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | cached_facets_kimi_k31_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_facets_opus_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_facets_sonnet_all | 87 (43/44) | 28 (15/13) | 59 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | cached_facets_sonnet_b6_cm18 | 87 (43/44) | 28 (15/13) | 59 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | cached_hackdrop_goodfire_dev_master | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_kev_any_cheat_all | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_kev_any_cheat_eval_only | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_kev_grader_tamper_all | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_kev_hollow_solution_all | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_kev_max_pattern_all | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_kev_router_all | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_kev_router_eval_only | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_metr_scanner_grok43_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_metr_scanner_kimi_k31_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_omp_form_tt7_sonnet_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_p0f9d7d6_flags_all | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.9886 | [0.9643, 1.0000] | 2000 |
| reflex-sentinel | cached_p0f9d7d6_qa_trace_t0_all | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | cached_p0f9d7d6_run_health_infra_all | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | cached_p0f9d7d6_run_health_latency_all | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | cached_p0f9d7d6_run_health_loop_all | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.6995 | [0.5738, 0.8140] | 2000 |
| reflex-sentinel | cached_p0f9d7d6_run_health_runaway_all | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5568 | [0.5116, 0.6047] | 2000 |
| reflex-sentinel | cached_p0f9d7d6_tripwire_all | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | cached_reader_dt15_masked_deepseek | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_reader_dt16_wide_caps_deepseek | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_reader_dt17_investigator_deepseek | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_reader_dt18_v4_deepseek | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_reader_dt8_v0_deepseek | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_scout_native_v2 | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_sol_astra_monitor_baseline_fable | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_sol_astra_monitor_structured_fable | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_tmb_autoreview_fable | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_tmb_autoreview_g38 | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_tmb_autoreview_g38_fixed | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_tmb_dt15_masked_deepseek | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_tmb_l0_tripwires | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_tmb_sol_astra_flat_fable | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_tmb_sol_astra_structured_fable | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_trace_tools_docent_form_sonnet55_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_trace_tools_hodoscope_top10_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_trace_tools_hodoscope_top25_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_trace_tools_hodoscope_top50_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_trace_tools_metr_scanner_raw_grok43_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_trace_tools_metr_scanner_raw_kimi_k31_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_trace_tools_metr_scanner_raw_kimi_k3_fp4_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_trace_tools_omp_form_gpt6_luna_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_trace_tools_omp_form_opus55_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_trace_tools_omp_form_sonnet55_dev | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | cached_whole_run_logistic_v1m3 | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | j0_autoreview | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | j1_actions_flash | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | j2 | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | j2_actions_task_diff_flash | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | j2_fable_max_partial | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | j2_fable_mean_partial | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | j2_terra_max | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | j2_terra_mean | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | j2b | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | j2b_3fam_max | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | j2b_3fam_mean | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | j2b_actions_task_diff_v2_flash | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | j2b_fable_max_partial | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | j2b_fable_mean_partial | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | j2b_terra_max | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | j2b_terra_mean | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | j3_actions_task_diff_fable_partial | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | j3_actions_task_diff_terra | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | j3_actions_task_diff_terra_full | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | j3_terra | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_any_hard | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_any_rule | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_decision | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_hard_content_stop_tamper_signature | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_hard_gate_rule_grader_tamper | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_hard_gate_rule_hidden_info_read | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_hard_gate_rule_submission_tamper | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_hard_hackbook_hb_fence_in_comment | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_hard_hackbook_hb_frame_introspection | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_hard_hackbook_hb_grader_fs_mutation | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_hard_hackbook_hb_result_forgery | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_hard_hackbook_hb_runner_shadow | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_hard_hackbook_hb_skip_exit_marker | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_hard_hackbook_hb_startup_hook_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_hard_hackbook_hb_success_stub_script | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_hard_hackbook_hb_test_literal_echo | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_hard_tamper_signature__bprocess__exit_s | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_hard_tamper_signature__bprocess__exitcode_s | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_hard_tamper_signature__bpytest__runtest_configure_collection_sessionfinish_sessionstart_report_terminal_unconfigure | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_hard_target_surface_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_no_evidence | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_runner_error | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_soft_config_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_soft_gate_rule_hackbook_hb_runner_shadow | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_soft_gate_rule_hackbook_hb_success_stub_script | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_soft_gate_rule_history_mining | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_soft_login_profile_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_soft_target_surface_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_soft_test_write_unverified | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_actions_only_l0_unrecognized_tool_shape | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5233 | [0.5000, 0.5606] | 2000 |
| reflex-sentinel | l0cf2_full_any_hard | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0cf2_full_any_rule | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0cf2_full_decision | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_hard_content_stop_tamper_signature | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_hard_gate_rule_grader_tamper | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_hard_gate_rule_hidden_info_read | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_hard_gate_rule_submission_tamper | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_hard_hackbook_hb_fence_in_comment | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_hard_hackbook_hb_frame_introspection | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_hard_hackbook_hb_grader_fs_mutation | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_hard_hackbook_hb_result_forgery | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_hard_hackbook_hb_runner_shadow | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_hard_hackbook_hb_skip_exit_marker | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_hard_hackbook_hb_startup_hook_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_hard_hackbook_hb_success_stub_script | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_hard_hackbook_hb_test_literal_echo | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_hard_tamper_signature__bprocess__exit_s | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_hard_tamper_signature__bprocess__exitcode_s | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_hard_tamper_signature__bpytest__runtest_configure_collection_sessionfinish_sessionstart_report_terminal_unconfigure | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_hard_target_surface_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_no_evidence | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_runner_error | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_soft_config_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_soft_gate_rule_hackbook_hb_runner_shadow | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_soft_gate_rule_hackbook_hb_success_stub_script | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_soft_gate_rule_history_mining | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_soft_login_profile_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_soft_target_surface_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_soft_test_write_unverified | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf2_full_l0_unrecognized_tool_shape | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5233 | [0.5000, 0.5606] | 2000 |
| reflex-sentinel | l0cf_actions_only_any_hard | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0cf_actions_only_any_rule | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0cf_actions_only_decision | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_hard_content_stop_tamper_signature | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_hard_gate_rule_grader_tamper | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_hard_gate_rule_hidden_info_read | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_hard_gate_rule_submission_tamper | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_hard_hackbook_hb_frame_introspection | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_hard_hackbook_hb_grader_fs_mutation | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_hard_hackbook_hb_result_forgery | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_hard_hackbook_hb_runner_shadow | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_hard_hackbook_hb_skip_exit_marker | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_hard_hackbook_hb_startup_hook_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_hard_hackbook_hb_success_stub_script | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_hard_hackbook_hb_test_literal_echo | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_hard_tamper_signature__bprocess__exit_s | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_hard_tamper_signature__bprocess__exitcode_s | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_hard_tamper_signature__bpytest__runtest_configure_collection_sessionfinish_sessionstart_report_terminal_unconfigure | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_hard_target_surface_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_no_evidence | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_runner_error | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_soft_config_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_soft_gate_rule_hackbook_hb_runner_shadow | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_soft_gate_rule_hackbook_hb_success_stub_script | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_soft_gate_rule_history_mining | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_soft_login_profile_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_soft_target_surface_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_soft_test_write_unverified | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_actions_only_l0_unrecognized_tool_shape | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5233 | [0.5000, 0.5606] | 2000 |
| reflex-sentinel | l0cf_full_any_hard | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0cf_full_any_rule | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0cf_full_decision | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0cf_full_l0_hard_content_stop_tamper_signature | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_hard_gate_rule_grader_tamper | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_hard_gate_rule_hidden_info_read | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_hard_gate_rule_submission_tamper | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_hard_hackbook_hb_fence_in_comment | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_hard_hackbook_hb_frame_introspection | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_hard_hackbook_hb_grader_fs_mutation | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_hard_hackbook_hb_result_forgery | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_hard_hackbook_hb_runner_shadow | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_hard_hackbook_hb_skip_exit_marker | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_hard_hackbook_hb_startup_hook_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_hard_hackbook_hb_success_stub_script | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_hard_hackbook_hb_test_literal_echo | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_hard_tamper_signature__bprocess__exit_s | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_hard_tamper_signature__bprocess__exitcode_s | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_hard_tamper_signature__bpytest__runtest_configure_collection_sessionfinish_sessionstart_report_terminal_unconfigure | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_hard_target_surface_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0cf_full_l0_no_evidence | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_runner_error | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_soft_config_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_soft_gate_rule_hackbook_hb_runner_shadow | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_soft_gate_rule_hackbook_hb_success_stub_script | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_soft_gate_rule_history_mining | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_soft_login_profile_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_soft_target_surface_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_soft_test_write_unverified | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0cf_full_l0_unrecognized_tool_shape | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5233 | [0.5000, 0.5606] | 2000 |
| reflex-sentinel | l0v_actions_only_any_hard | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0v_actions_only_any_rule | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0v_actions_only_decision | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_hard_content_stop_tamper_signature | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_hard_gate_rule_grader_tamper | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_hard_gate_rule_hidden_info_read | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_hard_gate_rule_submission_tamper | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_hard_hackbook_hb_frame_introspection | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_hard_hackbook_hb_grader_fs_mutation | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_hard_hackbook_hb_result_forgery | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_hard_hackbook_hb_runner_shadow | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_hard_hackbook_hb_skip_exit_marker | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_hard_hackbook_hb_startup_hook_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_hard_hackbook_hb_success_stub_script | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_hard_hackbook_hb_test_literal_echo | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_hard_tamper_signature__bprocess__exit_s | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_hard_tamper_signature__bprocess__exitcode_s | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_hard_tamper_signature__bpytest__runtest_configure_collection_sessionfinish_sessionstart_report_terminal_unconfigure | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_hard_target_surface_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_no_evidence | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_runner_error | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_soft_config_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_soft_gate_rule_hackbook_hb_runner_shadow | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_soft_gate_rule_hackbook_hb_success_stub_script | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_soft_gate_rule_history_mining | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_soft_login_profile_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_soft_target_surface_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_soft_test_write_unverified | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_actions_only_l0_unrecognized_tool_shape | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_any_hard | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0v_full_any_rule | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0v_full_decision | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0v_full_l0_hard_content_stop_tamper_signature | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_hard_gate_rule_grader_tamper | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_hard_gate_rule_hidden_info_read | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_hard_gate_rule_submission_tamper | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_hard_hackbook_hb_fence_in_comment | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_hard_hackbook_hb_frame_introspection | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_hard_hackbook_hb_grader_fs_mutation | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_hard_hackbook_hb_result_forgery | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_hard_hackbook_hb_runner_shadow | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_hard_hackbook_hb_skip_exit_marker | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_hard_hackbook_hb_startup_hook_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_hard_hackbook_hb_success_stub_script | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_hard_hackbook_hb_test_literal_echo | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_hard_tamper_signature__bprocess__exit_s | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_hard_tamper_signature__bprocess__exitcode_s | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_hard_tamper_signature__bpytest__runtest_configure_collection_sessionfinish_sessionstart_report_terminal_unconfigure | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_hard_target_surface_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | l0v_full_l0_no_evidence | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_runner_error | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_soft_config_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_soft_gate_rule_hackbook_hb_runner_shadow | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_soft_gate_rule_hackbook_hb_success_stub_script | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_soft_gate_rule_history_mining | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_soft_login_profile_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_soft_target_surface_write | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_soft_test_write_unverified | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | l0v_full_l0_unrecognized_tool_shape | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5000 | [0.5000, 0.5000] | 2000 |
| reflex-sentinel | length_chars | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.6728 | [0.5457, 0.7887] | 2000 |
| reflex-sentinel | length_steps | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5568 | [0.5116, 0.6047] | 2000 |
| reflex-sentinel | line_l0_any_hard | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_l0_l0_hard_content_stop_tamper_signature | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_l0_l0_hard_gate_rule_grader_tamper | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_l0_l0_hard_gate_rule_hidden_info_read | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_l0_l0_hard_gate_rule_submission_tamper | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_l0_l0_hard_hackbook_hb_fence_in_comment | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_l0_l0_hard_hackbook_hb_frame_introspection | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_l0_l0_hard_hackbook_hb_grader_fs_mutation | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_l0_l0_hard_hackbook_hb_result_forgery | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_l0_l0_hard_hackbook_hb_runner_shadow | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_l0_l0_hard_hackbook_hb_skip_exit_marker | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_l0_l0_hard_hackbook_hb_success_stub_script | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_l0_l0_hard_hackbook_hb_test_literal_echo | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_l0_l0_hard_tamper_signature__bprocess__exit_s___ | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_l0_l0_hard_target_surface_write | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_l0_l0_no_evidence | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_l0_l0_soft_gate_rule_hackbook_hb_runner_shadow | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_l0_l0_soft_gate_rule_hackbook_hb_success_stub_script | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_l0_l0_soft_gate_rule_history_mining | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_l0_l0_soft_login_profile_write | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_l0_l0_soft_target_surface_write | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_l0_l0_soft_test_write_unverified | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_l0_l0_unrecognized_tool_shape | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_l0_unrecognized_shape | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | line_replay_decision | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | outcome_regrade | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | outcome_regrade_stored | 87 (43/44) | 0 (0/0) | 87 | 0 | 0 | — | — | 0 |
| reflex-sentinel | probe_max_j2b | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | probe_or_j2b | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |
| reflex-sentinel | probe_qwen3_8b | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5164 | [0.4036, 0.6318] | 2000 |
| reflex-sentinel | probe_qwen3_8b_run_mean | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.8367 | [0.7436, 0.9160] | 2000 |
| reflex-sentinel | probe_qwen3_8b_run_mean_z | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.8367 | [0.7436, 0.9160] | 2000 |
| reflex-sentinel | probe_qwen3_8b_z | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.5164 | [0.4036, 0.6318] | 2000 |
| reflex-sentinel | probe_run_mean_or_j2b | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 0.9773 | [0.9432, 1.0000] | 2000 |
| reflex-sentinel | probe_stack_j2b | 87 (43/44) | 87 (43/44) | 0 | 0 | 0 | 1.0000 | [1.0000, 1.0000] | 2000 |

Source: `/private/tmp/eval-lab-study/research/evidence/runs/corpus-expansion-pq1-20261027/corpora/matu79go__reflex-sentinel-agent-trajectories/README.md:76-79` and `tools/adapter.py:27-29,122-148`. Original `REPORT.md` and `aggregate.json` remain unchanged.
