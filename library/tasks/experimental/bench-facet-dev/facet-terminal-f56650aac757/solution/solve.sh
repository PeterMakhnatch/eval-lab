#!/bin/bash
set -e
cd /task_file

python3 - <<'PY'
import json
import pathlib
import re

def load_json(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def load_text(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()

def load_jsonl(path):
    events = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events

def redact_external_urls(obj):
    """Recursively replace http:// and https:// URL strings with a redacted placeholder."""
    if isinstance(obj, dict):
        return {k: redact_external_urls(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [redact_external_urls(item) for item in obj]
    elif isinstance(obj, str):
        if obj.startswith('http://') or obj.startswith('https://'):
            return '[REDACTED_EXTERNAL_URL]'
        return obj
    else:
        return obj

def redact_urls_in_text(text):
    """Replace any http:// or https:// URLs found within a text string."""
    return re.sub(r'https?://[^\s]+', '[REDACTED_EXTERNAL_URL]', text)

input_dir = pathlib.Path('/task_file/input')

# 1. model_generation
model_gen = load_json(input_dir / 'chat_completion_response.json')

# 2. notebook_artifact
notebook = load_json(input_dir / 'formatted_notebook.ipynb')
notebook_meta = load_json(input_dir / 'notebook_enrichment_metadata.json')
notebook_artifact = {
    'notebook': notebook,
    'enrichment_metadata': notebook_meta
}

# 3. social_media_post
social_post = load_json(input_dir / 'xiaohongshu_post_payload.json')

# 4. community_publication
community_pub = load_json(input_dir / 'moltbook_publish_confirmation.json')
community_pub = redact_external_urls(community_pub)

# 5. watermarked_pdf
watermarked_pdf = load_json(input_dir / 'watermarked_pdf_metadata.json')

# 6. cloud_scan_primary
cloud_scan_primary_text = load_text(input_dir / 'lianke_scan_primary.log')
# Parse log lines into structured events
primary_events = []
for line in cloud_scan_primary_text.strip().split('\n'):
    if line.strip():
        primary_events.append(line)
cloud_scan_primary = {
    'raw_log': cloud_scan_primary_text,
    'events': primary_events
}

# 7. cloud_scan_redundant
cloud_scan_redundant_text = load_text(input_dir / 'lianke_scan_redundant.log')
redundant_events = []
for line in cloud_scan_redundant_text.strip().split('\n'):
    if line.strip():
        redundant_events.append(line)
cloud_scan_redundant = {
    'raw_log': cloud_scan_redundant_text,
    'events': redundant_events
}

# 8. image_editing
image_edit_summary = load_text(input_dir / 'ideogram_edit_summary.txt')

# 9. service_health - redact any URLs in the raw text
clawnexus_transcript = load_text(input_dir / 'clawnexus_status_transcript.txt')
clawnexus_transcript = redact_urls_in_text(clawnexus_transcript)

# 10. adaptive_learning
adaptive_events = load_jsonl(input_dir / 'reflexlearn_observation_log.jsonl')

# 11. market_context
market_context = load_json(input_dir / 'polymarket_trending_snapshot.json')

# 12. session_archival
session_transcript = load_text(input_dir / 'session_management_transcript.txt')

# 13. fixture_provenance
fixture_manifest = load_json(input_dir / 'fixture_manifest.json')

pipeline_record = {
    'model_generation': model_gen,
    'notebook_artifact': notebook_artifact,
    'social_media_post': social_post,
    'community_publication': community_pub,
    'watermarked_pdf': watermarked_pdf,
    'cloud_scan_primary': cloud_scan_primary,
    'cloud_scan_redundant': cloud_scan_redundant,
    'image_editing': image_edit_summary,
    'service_health': clawnexus_transcript,
    'adaptive_learning': adaptive_events,
    'market_context': market_context,
    'session_archival': session_transcript,
    'fixture_provenance': fixture_manifest
}

output_path = pathlib.Path('/task_file/pipeline_record.json')
with open(output_path, 'w', encoding='utf-8') as f:
    json.dump(pipeline_record, f, indent=2, ensure_ascii=False)

# Self-checks
print("Running self-checks...")
assert output_path.exists(), "pipeline_record.json not found"
with open(output_path, 'r', encoding='utf-8') as f:
    check = json.load(f)
assert isinstance(check, dict), "Top-level is not object"
required_keys = [
    'model_generation',
    'notebook_artifact',
    'social_media_post',
    'community_publication',
    'watermarked_pdf',
    'cloud_scan_primary',
    'cloud_scan_redundant',
    'image_editing',
    'service_health',
    'adaptive_learning',
    'market_context',
    'session_archival',
    'fixture_provenance'
]
for key in required_keys:
    assert key in check, f"Missing required key: {key}"
assert isinstance(check['adaptive_learning'], list), "adaptive_learning must be array"
assert isinstance(check['notebook_artifact'], dict), "notebook_artifact must be object"
assert 'notebook' in check['notebook_artifact'], "notebook_artifact missing notebook"
assert 'enrichment_metadata' in check['notebook_artifact'], "notebook_artifact missing enrichment_metadata"
print("All self-checks passed.")
PY
