#!/usr/bin/env python3
"""Generate realistic pipeline fixture files for Step3 v2 environment."""
import json, os, hashlib, uuid, random
from datetime import datetime, timedelta, timezone

FIXTURE_DIR = "/task_file/input"
os.makedirs(FIXTURE_DIR, exist_ok=True)

def ts(offset_minutes=0):
    return (datetime.now(timezone.utc) - timedelta(minutes=offset_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

def write_json(name, data):
    with open(os.path.join(FIXTURE_DIR, name), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def write_text(name, content):
    with open(os.path.join(FIXTURE_DIR, name), "w", encoding="utf-8") as f:
        f.write(content)

# 1. Chat completion response (RunAPI captured)
write_json("chat_completion_response.json", {
    "request_id": str(uuid.uuid4()),
    "captured_at": ts(120),
    "source": "runapi-gateway",
    "api_version": "v1",
    "model": "gemini-2.0-flash-exp",
    "request_params": {
        "temperature": 0.7,
        "max_tokens": 4096,
        "top_p": 0.95,
        "stream": False
    },
    "response_metadata": {
        "latency_ms": 2341,
        "tokens_used": {"prompt": 312, "completion": 847, "total": 1159},
        "finish_reason": "STOP"
    },
    "warnings": ["content_filter_confidence: 0.92"],
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "## Market Pulse: Decentralized AI Agents in Q1 2026\n\nThe convergence of onchain execution and large language models is accelerating. We observe three major shifts this quarter:\n\n1. **Agent-to-Agent Negotiation** — Protocols like Clawnexus now support structured bargaining over API access windows, with settlement in USDC.\n2. **Watermarking Standardization** — The IETF draft for LLM output watermarking has gained 14 new co-sponsors, pushing toward RFC status.\n3. **Social-First Content Pipelines** — Platforms like Xiaohongshu and Moltbook are building native hooks for AI-disclosed posts, requiring explicit model provenance in metadata.\n\nThese trends suggest that content authenticity tooling will become a default layer in publishing workflows by Q3 2026."
            },
            "finish_reason": "STOP",
            "safety_ratings": [
                {"category": "HARM_CATEGORY_HARASSMENT", "probability": "NEGLIGIBLE"},
                {"category": "HARM_CATEGORY_HATE_SPEECH", "probability": "NEGLIGIBLE"}
            ]
        }
    ],
    "usage": {"prompt_tokens": 312, "completion_tokens": 847, "total_tokens": 1159}
})

# 2. Formatted notebook artifact and enrichment metadata
write_json("notebook_enrichment_metadata.json", {
    "artifact_id": "nb-9f3a2b1c",
    "source_prompt_hash": hashlib.sha256(b"market pulse q1 2026").hexdigest(),
    "enrichment_pipeline": "notebook-formatter-v2.4",
    "captured_at": ts(115),
    "request_id": str(uuid.uuid4()),
    "transformations": [
        "markdown_to_ipynb",
        "code_block_tagging",
        "citation_injection",
        "toc_generation"
    ],
    "notebook_cells": 8,
    "injected_citations": ["arxiv:2401.12345", "ietf-draft-watermark-03"],
    "output_formats": ["ipynb", "html", "pdf"],
    "status": "enriched",
    "warnings": []
})
write_text("formatted_notebook.ipynb", json.dumps({
    "cells": [
        {"cell_type": "markdown", "source": ["# Market Pulse: Decentralized AI Agents"]},
        {"cell_type": "code", "source": ["print('Hello from notebook')"], "outputs": []}
    ],
    "metadata": {"kernelspec": {"display_name": "Python 3"}},
    "nbformat": 4, "nbformat_minor": 5
}, indent=2))

# 3. Xiaohongshu post payload with code screenshot mappings
write_json("xiaohongshu_post_payload.json", {
    "request_id": str(uuid.uuid4()),
    "captured_at": ts(110),
    "platform": "xiaohongshu",
    "post_type": "article_with_code_screenshots",
    "author_id": "ai_pipeline_bot_01",
    "content": {
        "title": "AI Agents Are Trading Onchain Now 🤖",
        "body": "Check out how decentralized agents negotiate API access...",
        "hashtags": ["#AI", "#web3", "#automation"],
        "code_screenshot_mappings": [
            {"image_id": "img_001", "source_cell": 1, "caption": "Agent negotiation loop"},
            {"image_id": "img_002", "source_cell": 3, "caption": "Watermark verification snippet"}
        ]
    },
    "publish_status": "review_passed",
    "scheduled_at": ts(90),
    "api_version": "v2.1",
    "trace_id": str(uuid.uuid4())
})

# 4. Moltbook community publish-confirmation response
write_json("moltbook_publish_confirmation.json", {
    "request_id": str(uuid.uuid4()),
    "captured_at": ts(105),
    "community_id": "mol_ai_dev",
    "post_slug": "decentralized-ai-agents-q1-2026",
    "publish_result": {
        "status": "published",
        "url": "https://moltbook.io/p/decentralized-ai-agents-q1-2026",
        "published_at": ts(100),
        "content_hash": hashlib.sha256(b"moltbook body v1").hexdigest()
    },
    "moderation_flags": [],
    "rewards_eligible": True,
    "pagination": {"page": 1, "total_pages": 1}
})

# 5. Watermarked PDF metadata
write_json("watermarked_pdf_metadata.json", {
    "file_path": "/artifacts/notebook_watermarked.pdf",
    "watermark_id": "wm-7d3f4e5a",
    "applied_at": ts(98),
    "checksum_sha256": hashlib.sha256(b"watermarked pdf content v1").hexdigest(),
    "watermark_algorithm": "ietf-draft-watermark-03",
    "payload": {"model": "gemini-2.0-flash-exp", "timestamp": ts(120), "org": "pipeline-lab"},
    "page_count": 4,
    "file_size_bytes": 128450
})

# 6. Lianke scan task lifecycle logs (two scans)
def write_scan_log(filename, scan_id, is_redundant=False):
    base_time = datetime.now(timezone.utc) - timedelta(minutes=95)
    lines = [
        f"{base_time.strftime('%Y-%m-%dT%H:%M:%SZ')} [INFO] LiankeScanScheduler: Creating scan task scan_id={scan_id} target=notebook_watermarked.pdf",
        f"{base_time.strftime('%Y-%m-%dT%H:%M:%SZ')} [INFO] LiankeWorker: Task {scan_id} queued priority=high",
        f"{(base_time + timedelta(seconds=30)).strftime('%Y-%m-%dT%H:%M:%SZ')} [INFO] LiankeWorker: Polling status for {scan_id} -> PROCESSING",
        f"{(base_time + timedelta(seconds=60)).strftime('%Y-%m-%dT%H:%M:%SZ')} [INFO] LiankeWorker: Polling status for {scan_id} -> COMPLETED",
        f"{(base_time + timedelta(seconds=62)).strftime('%Y-%m-%dT%H:%M:%SZ')} [INFO] LiankeWorker: Retrieving scan result for {scan_id}",
        f"{(base_time + timedelta(seconds=65)).strftime('%Y-%m-%dT%H:%M:%SZ')} [INFO] LiankeWorker: Result saved to /scans/{scan_id}.json"
    ]
    if is_redundant:
        lines.append(f"{(base_time + timedelta(seconds=70)).strftime('%Y-%m-%dT%H:%M:%SZ')} [WARN] LiankeScheduler: Redundant scan {scan_id} completed (duplicate of earlier task)")
    write_text(filename, "\n".join(lines))

write_scan_log("lianke_scan_primary.log", "lianke-scan-001")
write_scan_log("lianke_scan_redundant.log", "lianke-scan-002", is_redundant=True)

# 7. Ideogram V3 edit result summary (CLI dry-run output)
write_text("ideogram_edit_summary.txt", f"""[DRY-RUN] Ideogram CLI v3.2.1
Request ID: {uuid.uuid4()}
Phase: preview
Input image: notebook_screenshot.png
Edit prompt: "Add watermark badge and border"
Estimated cost: $0.04
Validation: PASSED
Policy warnings: content_filter_score=0.12
Asset probe: resolution=1024x768, format=png
Normalized body: {{"operations": ["watermark_overlay", "border_radius:8px"]}}
Ignored fields: ["metadata.original_camera"]
""")

# 8. Clawnexus instance status and connection URL transcript
write_text("clawnexus_status_transcript.txt", f"""[clawnexus-cli] instance status --id i-9f3a2b1c
Instance ID: i-9f3a2b1c
State: RUNNING
Public URL: https://i-9f3a2b1c.clawnexus.cloud/gateway
Health: HEALTHY
Uptime: 14d 3h 22m
Connection pool: 8/20 active
Last health check: {ts(10)}
""")

# 9. ReflexLearn observation event log
write_text("reflexlearn_observation_log.jsonl", "\n".join([
    json.dumps({"event": "observation", "timestamp": ts(80), "agent": "pipeline-monitor", "metric": "latency_p95", "value": 2341, "tags": ["chat_completion"]}),
    json.dumps({"event": "observation", "timestamp": ts(75), "agent": "pipeline-monitor", "metric": "publish_success_rate", "value": 0.98, "tags": ["moltbook", "xiaohongshu"]}),
    json.dumps({"event": "observation", "timestamp": ts(70), "agent": "pipeline-monitor", "metric": "scan_duplicates", "value": 1, "tags": ["lianke", "redundancy"]}),
    json.dumps({"event": "observation", "timestamp": ts(65), "agent": "adaptive-learner", "metric": "model_drift", "value": 0.002, "tags": ["gemini"]}),
    json.dumps({"event": "observation", "timestamp": ts(60), "agent": "adaptive-learner", "metric": "watermark_detection_rate", "value": 0.999, "tags": ["ietf-draft"]})
]))

# 10. Polymarket onchain CLI snapshot of trending markets
write_json("polymarket_trending_snapshot.json", {
    "cli_version": "polymarket-cli v0.9.2",
    "captured_at": ts(50),
    "chain": "polygon",
    "block_number": 48720000,
    "markets": [
        {
            "id": "0xabc123",
            "question": "Will IETF adopt LLM watermarking RFC by Q3 2026?",
            "volume_usdc": 245000,
            "probability": 0.68,
            "trend": "up",
            "tags": ["AI", "standards"]
        },
        {
            "id": "0xdef456",
            "question": "Will a decentralized AI agent complete a $1M+ trade by Q4 2026?",
            "volume_usdc": 189000,
            "probability": 0.42,
            "trend": "stable",
            "tags": ["AI", "defi"]
        },
        {
            "id": "0x789ghi",
            "question": "Will Xiaohongshu require AI disclosure labels by 2027?",
            "volume_usdc": 67000,
            "probability": 0.81,
            "trend": "up",
            "tags": ["social", "regulation"]
        }
    ],
    "pagination": {"page": 1, "total_pages": 3},
    "warnings": ["rate_limit_remaining: 42"]
})

# 11. Session management transcript
write_text("session_management_transcript.txt", f"""[session-mgr] list transcripts
  - original: session_2026-01-15_main.json
  - original: session_2026-01-15_qa.json
  - original: session_2026-01-15_debug.json

[session-mgr] rename session_2026-01-15_main.json -> DEL_20260115_120000_main.json
  status: OK
[session-mgr] rename session_2026-01-15_qa.json -> DEL_20260115_120001_qa.json
  status: OK
[session-mgr] rename session_2026-01-15_debug.json -> DEL_20260115_120002_debug.json
  status: OK

[session-mgr] confirm-deletion --target DEL_20260115_120001_qa.json
  Are you sure? [y/N]: y
  Deletion confirmed for DEL_20260115_120001_qa.json
  Audit log: /var/log/session-mgr/audit.log updated
""")

print("Fixtures generated successfully.")
