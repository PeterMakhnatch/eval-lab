-- CRAFT views over the deterministic task-corpus scan (WS-A).
--
-- Source: derived/parquet/craft/craft.parquet, written by
-- `python -m evallab.craft scan --all-local`. Rebuildable: nothing here is
-- authoritative, and dropping the Parquet file costs one scan.
--
-- DuckDB, not PostgreSQL. `sql/schema.sql` is the catalog's PostgreSQL schema;
-- the craft corpus stays in Parquet because it is a projection of files on disk
-- rather than of run history, and DuckDB reads it without an ingest step.
--
-- Run from the repository root:
--
--   duckdb -c ".read sql/craft_views.sql" -c "SELECT * FROM v_craft_verifier_type"
--
-- Or from anywhere, by naming the file first:
--
--   SET VARIABLE craft_parquet = '/abs/path/derived/parquet/craft/craft.parquet';
--   .read sql/craft_views.sql
--
-- Every view carries `source_repo` so the TB3 corpus and the in-repository
-- library are never silently pooled: they answer different questions, and their
-- facet coverage differs (TB3 states a human time anchor on every task; the
-- adapter-generated library shards state none).

SET VARIABLE craft_parquet = coalesce(
    getvariable('craft_parquet'),
    'derived/parquet/craft/craft.parquet'
);

CREATE OR REPLACE VIEW craft AS
SELECT * FROM read_parquet(getvariable('craft_parquet'));

-- The acceptance query for WS-A: what do these verifiers actually do?
-- `verifier_type IS NULL` is a first-class row, not missing data: it means the
-- verifier's mechanism is real but unnameable in the spec's enum. Join
-- v_craft_verifier_signals to see which mechanism it was.
CREATE OR REPLACE VIEW v_craft_verifier_type AS
SELECT
    source_repo,
    coalesce(verifier_type, 'unclassified') AS verifier_type,
    count(*) AS tasks,
    round(100.0 * count(*) / sum(count(*)) OVER (PARTITION BY source_repo), 1) AS pct_of_corpus
FROM craft
GROUP BY source_repo, verifier_type
ORDER BY source_repo, tasks DESC, verifier_type;

-- The structural evidence behind `verifier_type`. `pytest`, `diff`,
-- `golden_file`, and `judge` are the spec's enum; `unit_js`, `shell_only`, and
-- `scorer_script` are mechanisms the corpus contains and the enum cannot name.
CREATE OR REPLACE VIEW v_craft_verifier_signals AS
SELECT
    source_repo,
    signal,
    count(*) AS tasks
FROM craft, unnest(verifier_signals) AS t(signal)
GROUP BY source_repo, signal
ORDER BY source_repo, tasks DESC, signal;

-- Anti-cheat technique adoption. `hidden_tests` and `answer_outside_image` come
-- from the same evidence (`[verifier].environment_mode = "separate"`), so they
-- move together by construction; `digest_check` and `process_check` are
-- independent observations of the verifier's own code.
CREATE OR REPLACE VIEW v_craft_anti_cheat AS
SELECT
    source_repo,
    technique,
    count(*) AS tasks
FROM craft, unnest(anti_cheat) AS t(technique)
GROUP BY source_repo, technique
ORDER BY source_repo, tasks DESC, technique;

CREATE OR REPLACE VIEW v_craft_answer_hiding AS
SELECT
    source_repo,
    coalesce(answer_hiding, 'none_observed') AS answer_hiding,
    count(*) AS tasks
FROM craft
GROUP BY source_repo, answer_hiding
ORDER BY source_repo, tasks DESC, answer_hiding;

-- Environment shape. `env_services_n` is the compose service count, or 1 for a
-- lone Dockerfile; NULL means no environment declaration was found at all.
CREATE OR REPLACE VIEW v_craft_env_shape AS
SELECT
    source_repo,
    env_services_n,
    env_multi_container,
    count(*) AS tasks,
    min(env_n_files) AS min_env_files,
    round(avg(env_n_files), 1) AS avg_env_files,
    max(env_n_files) AS max_env_files
FROM craft
GROUP BY source_repo, env_services_n, env_multi_container
ORDER BY source_repo, env_services_n NULLS LAST;

CREATE OR REPLACE VIEW v_craft_env_languages AS
SELECT
    source_repo,
    language,
    count(*) AS tasks
FROM craft, unnest(env_languages) AS t(language)
GROUP BY source_repo, language
ORDER BY source_repo, tasks DESC, language;

-- Reproducibility of the environment build, in the two independent bits the
-- scan can establish: whether package versions are pinned, and how the base
-- image is referenced. A tag is not a pin.
CREATE OR REPLACE VIEW v_craft_reproducibility AS
SELECT
    source_repo,
    CASE
        WHEN pinned_deps IS NULL THEN 'no_dependency_declaration'
        WHEN pinned_deps THEN 'all_sites_pinned'
        ELSE 'some_site_unpinned'
    END AS dependency_pinning,
    coalesce(base_image_pin, 'no_dockerfile') AS base_image_pin,
    count(*) AS tasks
FROM craft
GROUP BY source_repo, pinned_deps, base_image_pin
ORDER BY source_repo, tasks DESC;

-- Human time anchors. Present only where the task states
-- `[metadata].expert_time_estimate_hours`; buckets are for triage, not analysis.
CREATE OR REPLACE VIEW v_craft_human_anchor AS
SELECT
    source_repo,
    human_minutes IS NOT NULL AS states_anchor,
    CASE
        WHEN human_minutes IS NULL THEN 'unstated'
        WHEN human_minutes < 60 THEN 'under_1h'
        WHEN human_minutes < 240 THEN '1h_to_4h'
        WHEN human_minutes < 480 THEN '4h_to_8h'
        ELSE 'over_8h'
    END AS anchor_bucket,
    count(*) AS tasks,
    min(human_minutes) AS min_minutes,
    max(human_minutes) AS max_minutes
FROM craft
GROUP BY source_repo, states_anchor, anchor_bucket
ORDER BY source_repo, tasks DESC, anchor_bucket;

-- The specification for the deferred LLM pass: which facets are null because
-- they are undeterminable from the bytes, and on how many tasks. `craft
-- classify` should consume exactly this view.
CREATE OR REPLACE VIEW v_craft_unresolved AS
SELECT
    source_repo,
    facet,
    count(*) AS tasks
FROM craft, unnest(unresolved_facets) AS t(facet)
GROUP BY source_repo, facet
ORDER BY source_repo, tasks DESC, facet;

-- One-line-per-corpus roll-up: the headline table.
CREATE OR REPLACE VIEW v_craft_corpus AS
SELECT
    source_repo,
    any_value(facets_schema_version) AS facets_schema_version,
    count(*) AS tasks,
    count(verifier_type) AS verifier_classified,
    count(*) - count(verifier_type) AS verifier_unclassified,
    sum(CASE WHEN env_multi_container THEN 1 ELSE 0 END) AS multi_container,
    sum(CASE WHEN pinned_deps THEN 1 ELSE 0 END) AS deps_pinned,
    sum(CASE WHEN base_image_pin = 'digest' THEN 1 ELSE 0 END) AS base_image_digest_pinned,
    count(human_minutes) AS states_human_anchor,
    count(instruction_style) AS instruction_style_known,
    count(difficulty_mechanism) AS difficulty_mechanism_known,
    round(avg(instruction_chars), 0) AS avg_instruction_chars
FROM craft
GROUP BY source_repo
ORDER BY source_repo;
