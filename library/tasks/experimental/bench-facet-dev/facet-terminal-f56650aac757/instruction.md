# Objective

Produce a unified Pipeline Execution Record at `/task_file/pipeline_record.json` that consolidates metadata from every stage of a captured content pipeline simulation. The record must be valid JSON and contain enough detail to reconstruct the sequence and verify consistency across stages without any live system access. All external service names are retained only as domain labels within the consolidated record.

# Inputs

All input files reside under `/task_file/input`. Every file is a captured response payload, dry-run output, status transcript, or provenance artifact from a specific pipeline stage. The following table lists every available file and its functional role:

| Absolute path | Role |
|---|---|
| `/task_file/input/chat_completion_response.json` | Primary source: raw model prompt and generated text from a RunAPI-captured chat completion (Gemini model output). |
| `/task_file/input/formatted_notebook.ipynb` | Primary source: enriched notebook-style document artifact. |
| `/task_file/input/notebook_enrichment_metadata.json` | Configuration/provenance metadata: enrichment details for the formatted notebook. |
| `/task_file/input/xiaohongshu_post_payload.json` | Primary source: social-media post payload with code-screenshot card mappings for Xiaohongshu. |
| `/task_file/input/moltbook_publish_confirmation.json` | Service response snapshot: Moltbook community publication confirmation metadata. |
| `/task_file/input/watermarked_pdf_metadata.json` | Provenance metadata: applied watermark identifier and file hash for the watermarked PDF. |
| `/task_file/input/lianke_scan_primary.log` | Service response snapshot: Lianke cloud-scan task lifecycle events for the primary scan (task creation, polling status transitions, retrieval confirmation). |
| `/task_file/input/lianke_scan_redundant.log` | Service response snapshot: Lianke cloud-scan task lifecycle events for the redundant second scan. |
| `/task_file/input/ideogram_edit_summary.txt` | Generated model output: Ideogram V3 image-editing result summary captured from CLI dry-run output. |
| `/task_file/input/clawnexus_status_transcript.txt` | Service response snapshot: Clawnexus instance status and connection URL transcript. |
| `/task_file/input/reflexlearn_observation_log.jsonl` | Adaptive-learning observation log: ReflexLearn event entries. |
| `/task_file/input/polymarket_trending_snapshot.json` | Report/analysis source: Polymarket onchain CLI snapshot of trending prediction-market topics. |
| `/task_file/input/session_management_transcript.txt` | Session archival actions: original transcript names, renamed deletion-timestamped filenames, and deletion confirmation for the target sub-session. |
| `/task_file/input/fixture_manifest.json` | Supporting evidence: manifest describing the pre-staged fixture files and their intended pipeline roles. |

# Required output

- **Path:** `/task_file/pipeline_record.json`
- **Format:** Valid JSON. The top-level value must be an object.

# Required content

The top-level JSON object must contain clearly named fields for each pipeline stage. Each field must preserve enough detail from its corresponding source file(s) to reconstruct the stage and verify consistency. The following table defines every required top-level field, its source file, and the required content treatment.

| Top-level field | Source file(s) | Content requirements |
|---|---|---|
| `model_generation` | `/task_file/input/chat_completion_response.json` | Extract and include the original prompt and the generated text. Preserve model identity metadata if present. |
| `notebook_artifact` | `/task_file/input/formatted_notebook.ipynb` and `/task_file/input/notebook_enrichment_metadata.json` | Include the notebook content (cells, metadata) and the enrichment metadata. The enrichment metadata must be embedded or referenced by its key fields. |
| `social_media_post` | `/task_file/input/xiaohongshu_post_payload.json` | Extract the post content and the complete code-screenshot card mappings. |
| `community_publication` | `/task_file/input/moltbook_publish_confirmation.json` | Extract the publication confirmation metadata, including any identifiers, timestamps, and status fields. |
| `watermarked_pdf` | `/task_file/input/watermarked_pdf_metadata.json` | Include the watermark identifier and the file hash. |
| `cloud_scan_primary` | `/task_file/input/lianke_scan_primary.log` | Capture the task lifecycle events: creation, polling status transitions, and retrieval confirmation. Preserve timestamps and status codes. |
| `cloud_scan_redundant` | `/task_file/input/lianke_scan_redundant.log` | Capture the task lifecycle events for the redundant second scan with the same level of detail as the primary scan. |
| `image_editing` | `/task_file/input/ideogram_edit_summary.txt` | Include the full edit result summary. |
| `service_health` | `/task_file/input/clawnexus_status_transcript.txt` | Include the instance status and connection URL. |
| `adaptive_learning` | `/task_file/input/reflexlearn_observation_log.jsonl` | Include the complete observation event log as a structured array of event objects. |
| `market_context` | `/task_file/input/polymarket_trending_snapshot.json` | Include the trending markets snapshot, preserving all market entries and their fields. |
| `session_archival` | `/task_file/input/session_management_transcript.txt` | Extract and include the original transcript filenames, the renamed deletion-timestamped filenames, and the confirmed deletion target. |
| `fixture_provenance` | `/task_file/input/fixture_manifest.json` | Include the manifest content that describes the fixture set. |

# Source-to-output mapping

- Content from each source file must be mapped to exactly one top-level field as specified in the table above.
- All source-derived values must be included; no pipeline stage may be omitted.
- Raw source content may be summarized for readability but must preserve all key identifiers, status codes, timestamps, hashes, URLs, filenames, and structured mappings present in the originals.
- The `notebook_artifact` field must represent the relationship between `/task_file/input/formatted_notebook.ipynb` and `/task_file/input/notebook_enrichment_metadata.json` by embedding the enrichment metadata within the notebook representation or by including them as sibling sub-fields under the same top-level key.
- The two Lianke scan logs (`/task_file/input/lianke_scan_primary.log` and `/task_file/input/lianke_scan_redundant.log`) must appear in separate, clearly distinguishable fields (`cloud_scan_primary` and `cloud_scan_redundant`).
- The `session_archival` field must capture the original names, the renamed names, and the deletion confirmation from `/task_file/input/session_management_transcript.txt`.

# Formatting and validation constraints

- The file must be valid JSON parseable by a standard JSON parser.
- The top-level value must be a JSON object.
- Every field listed in the Required Content table must be present at the top level. No additional top-level fields are permitted unless they are sub-objects needed to structure the required content.
- All string values must be UTF-8 encoded.
- Timestamps and identifiers must be preserved exactly as they appear in the source files.
- The JSON must not contain any embedded binary data, external URLs pointing to live systems, or placeholder tokens that reference missing information.
- Array fields (such as adaptive-learning events, trending markets, scan lifecycle events, and card mappings) must be represented as JSON arrays.
- The output file must be written to exactly `/task_file/pipeline_record.json`.
