"""Modal + vLLM serving recipe for trained-breaker-v1 (breaker self-hosted arms).

Serves the two full-weight checkpoints from model_manifest.json on two
matched single-GPU vLLM servers (one Modal app per arm; vLLM serves one
model per server and 2 x ~67 GiB does not fit one H200):

  baseline  Qwen/Qwen3.6-35B-A3B                          -> evallab-breaker-base
  l1        rewardhack/qwen3.6-35b-a3b-hacksft-thinkoff-873rows-ep3 -> evallab-breaker-l1

CPU-local only: ``plan`` and ``help`` print configuration and the $0
pre-flight without touching Modal, the network, or weights. Deployment
(``modal deploy``) and weight download are paid/remote steps documented in
SERVING.md and MUST NOT run without Peter's current budget approval.

Patterns mirror tools/modal-mimo-serve/serve.py (pinned image weights,
Modal secret API key, scale-to-zero, redacted log relay) with the backend
switched to vLLM per the model card ("loads with transformers or vLLM
like the base model") and the upstream vLLM Qwen3.6 recipe.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

MANIFEST_ENV = "EVALLAB_BREAKER_MANIFEST"
MANIFEST_PATH = Path(
    os.environ.get(MANIFEST_ENV, str(Path(__file__).resolve().parent / "model_manifest.json"))
)
MODEL_MANIFEST = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
# One source of truth for HF commits and the linux/amd64 vLLM image digest.
VLLM_IMAGE = MODEL_MANIFEST["runtime"]["image"]
ARMS = {
    arm: {
        "hf_repo": model["hf_repo"],
        "revision": model["revision"],
        "app": MODEL_MANIFEST["serving"]["app_per_arm"][arm],
        "volume": MODEL_MANIFEST["serving"]["app_per_arm"][arm] + "-weights",
        "secret": MODEL_MANIFEST["serving"]["app_per_arm"][arm] + "-api-key",
    }
    for arm, model in MODEL_MANIFEST["models"].items()
}
MAX_MODEL_LEN = MODEL_MANIFEST["runtime"]["max_model_len"]
PORT = 8000
MINUTES = 60
WEIGHTS_ROOT = "/weights"
GPU = "H200"
MAX_NUM_SEQS = 8
SCALEDOWN_WINDOW_SECONDS = 300

COMPLETION_MARKER = ".evallab-download-complete.json"


def _expected_shard_files(model_dir: str) -> set[str]:
    """Shard filenames the pinned revision must contain (from its own index)."""
    index_path = Path(model_dir) / "model.safetensors.index.json"
    config_path = Path(model_dir) / "config.json"
    if not config_path.exists():
        raise RuntimeError(f"config.json absent at {model_dir}: snapshot incomplete")
    if not index_path.exists():
        raise RuntimeError(f"model.safetensors.index.json absent at {model_dir}")
    with open(index_path, encoding="utf-8") as handle:
        weight_map = json.load(handle)["weight_map"]
    return set(weight_map.values())


def _verify_snapshot_complete(spec: dict, model_dir: str) -> int:
    """Raise unless every shard the index names is present. Returns shard count."""
    expected = _expected_shard_files(model_dir)
    missing = sorted(name for name in expected if not (Path(model_dir) / name).exists())
    if missing:
        raise RuntimeError(f"incomplete snapshot at {model_dir}: missing {missing}")
    return len(expected)


def _write_completion_marker(spec: dict, model_dir: str, shards: int) -> str:
    """Atomically record a completed, verified download bound to repo+revision."""
    marker = Path(model_dir) / COMPLETION_MARKER
    payload = {"hf_repo": spec["hf_repo"], "revision": spec["revision"], "shards": shards}
    tmp = marker.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp, marker)
    return str(marker)


def _require_completion_marker(spec: dict, model_dir: str) -> None:
    """Fail closed unless a marker bound to this exact repo+revision exists."""
    marker = Path(model_dir) / COMPLETION_MARKER
    if not marker.exists():
        raise RuntimeError(
            f"no download-completion marker at {marker}; "
            "run download_weights to completion first (see SERVING.md)"
        )
    with open(marker, encoding="utf-8") as handle:
        record = json.load(handle)
    if record.get("hf_repo") != spec["hf_repo"] or record.get("revision") != spec["revision"]:
        raise RuntimeError(
            f"stale marker at {marker} (covers {record}); "
            "re-run download_weights for the pinned repo+revision"
        )


def arm_config(arm: str) -> dict:
    """Resolved, JSON-serializable deploy configuration for one arm."""
    try:
        spec = ARMS[arm]
    except KeyError:
        raise SystemExit(f"error: unknown arm {arm!r}; choose from {sorted(ARMS)}") from None
    model_dir = f"{WEIGHTS_ROOT}/{spec['hf_repo']}/{spec['revision']}"
    return {
        "arm": arm,
        "app": spec["app"],
        "image": VLLM_IMAGE,
        "hf_repo": spec["hf_repo"],
        "revision": spec["revision"],
        "served_model_name": spec["hf_repo"],
        "model_dir": model_dir,
        "volume": spec["volume"],
        "secret": f"{spec['secret']} (required key VLLM_API_KEY)",
        "gpu": f"1x {GPU}",
        "cpu": 4.0,
        "memory_mib": 16 * 1024,
        "vllm_flags": vllm_command(spec, model_dir, api_key="<from-secret>"),
        "scaling": {
            "min_containers": 0,
            "max_containers": 1,
            "buffer_containers": 0,
            "scaledown_window_seconds": SCALEDOWN_WINDOW_SECONDS,
            "max_num_seqs": MAX_NUM_SEQS,
        },
        "runtime": {
            "max_model_len": MAX_MODEL_LEN,
            "max_tokens_per_request": 16384,
            "enable_thinking": False,
            "temperature": 1.0,
            "top_p": 1.0,
            "top_k": -1,
        },
    }


def vllm_command(spec: dict, model_dir: str, api_key: str) -> list[str]:
    """The vLLM OpenAI-server launch command for one arm."""
    return [
        "vllm",
        "serve",
        model_dir,
        "--served-model-name",
        spec["hf_repo"],
        "--host",
        "0.0.0.0",
        "--port",
        str(PORT),
        "--dtype",
        "bfloat16",
        "--max-model-len",
        str(MAX_MODEL_LEN),
        "--max-num-seqs",
        str(MAX_NUM_SEQS),
        "--gpu-memory-utilization",
        "0.90",
        "--generation-config",
        MODEL_MANIFEST["runtime"]["generation_config"],
        # Thinking OFF at the server level (upstream recipe troubleshooting):
        # the L1 weights were trained with renderer qwen3_5_disable_thinking.
        "--default-chat-template-kwargs",
        json.dumps({"enable_thinking": False}),
        "--api-key",
        api_key,
    ]


def cmd_plan(args: argparse.Namespace) -> int:
    """$0 pre-flight: print resolved config + official-price cost. No Modal calls."""
    plans = [arm_config(arm) for arm in (["baseline", "l1"] if args.arm == "both" else [args.arm])]
    print(json.dumps(plans if args.arm == "both" else plans[0], indent=2))
    print()
    print("Cost basis (list prices; billable only after budget-approved deploy):")
    print("  server: 1x H200 $4.5396/h + 4 CPU x $0.04716/h + 16 GiB x $0.007992/h")
    print("        = ~$4.8561/h per arm app, per-second billing (modal.com/pricing).")
    print("  idle tail per warm period: fixed 300 s x hourly rate ~= $0.41.")
    print("  weight download: ~67.0 GiB/arm via a separate CPU-only function")
    print("        (no GPU allocated during download; duration and HF egress /")
    print("        Modal ingress rates unverified).")
    print("  volume storage: ~134.0 GiB total retained; $0 only within Modal's")
    print("        1 TiB/mo include, else $0.09/GiB-mo (~$12/mo).")
    print("See SERVING.md for the full estimate, shutdown, and Tinker comparison.")
    return 0


def cmd_help(_args: argparse.Namespace) -> int:
    print(__doc__)
    print("commands: plan | help")
    print("  plan --arm {baseline|l1|both}   print resolved config + cost basis ($0)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="breaker Modal+vLLM recipe (CPU-local)")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="print resolved config + cost basis ($0)")
    plan.add_argument("--arm", choices=["baseline", "l1", "both"], default="both")
    plan.set_defaults(func=cmd_plan)
    help_cmd = sub.add_parser("help", help="print module docs")
    help_cmd.set_defaults(func=cmd_help)
    args = parser.parse_args(argv)
    return args.func(args)


# ---------------------------------------------------------------------------
# Remote section: imported by Modal only (``modal deploy modal_serve.py``).
# Nothing below runs during CPU-local plan/help. Deployment is a paid step
# requiring Peter's current budget approval; see SERVING.md.
# ---------------------------------------------------------------------------

try:
    import modal  # type: ignore[import-not-found]

    _MODAL_AVAILABLE = True
except ImportError:  # CPU-local plan/help path
    _MODAL_AVAILABLE = False

if _MODAL_AVAILABLE:
    import subprocess
    import threading

    _ARM = os.environ.get("EVALLAB_BREAKER_ARM", "l1")
    spec = ARMS[_ARM]
    model_dir = f"{WEIGHTS_ROOT}/{spec['hf_repo']}/{spec['revision']}"
    image = (
        modal.Image.from_registry(VLLM_IMAGE)
        .add_local_file(MANIFEST_PATH, "/etc/evallab/breaker-models.json", copy=True)
        .env(
            {
                MANIFEST_ENV: "/etc/evallab/breaker-models.json",
                "HF_XET_HIGH_PERFORMANCE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "EVALLAB_BREAKER_ARM": _ARM,
            }
        )
    )
    weights = modal.Volume.from_name(spec["volume"], create_if_missing=True)
    app = modal.App(spec["app"])

    @app.function(
        image=image,
        volumes={WEIGHTS_ROOT: weights},
        cpu=4.0,
        memory=8 * 1024,
        timeout=30 * MINUTES,
    )
    def download_weights() -> str:
        """Fetch the pinned revision (resumable), verify all shards, mark complete."""
        from huggingface_hub import snapshot_download

        # Always resume the fetch: config.json alone cannot prove completeness.
        snapshot_download(
            repo_id=spec["hf_repo"],
            revision=spec["revision"],
            local_dir=Path(model_dir),
        )
        shards = _verify_snapshot_complete(spec, model_dir)
        marker = _write_completion_marker(spec, model_dir, shards)
        weights.commit()
        return f"downloaded+verified: {model_dir} ({shards} shards; {marker})"

    @app.server(
        image=image,
        gpu=GPU,
        cpu=4.0,
        memory=16 * 1024,
        volumes={WEIGHTS_ROOT: weights},
        secrets=[modal.Secret.from_name(spec["secret"], required_keys=["VLLM_API_KEY"])],
        min_containers=0,
        max_containers=1,
        buffer_containers=0,
        scaledown_window=SCALEDOWN_WINDOW_SECONDS,
        startup_timeout=20 * MINUTES,
        exit_grace_period=30,
        port=PORT,
        # Bearer auth protects inference, not cold-start charges from endpoint hits.
        # Explicit app shutdown remains mandatory; these are not dollar fences.
        unauthenticated=True,
    )
    class BreakerServer:
        @modal.enter()
        def start(self) -> None:
            # The GPU is already billable, even if this guard refuses startup.
            _require_completion_marker(spec, model_dir)
            api_key = os.environ["VLLM_API_KEY"]
            command = vllm_command(spec, model_dir, api_key)
            self.process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
            assert self.process.stdout is not None

            def _relay(stream) -> None:
                for raw in iter(stream.readline, b""):
                    sys.stdout.write(raw.decode("utf-8", "replace").replace(api_key, "<redacted>"))
                    sys.stdout.flush()

            threading.Thread(target=_relay, args=(self.process.stdout,), daemon=True).start()
            _wait_ready(self.process)

        @modal.exit()
        def stop(self) -> None:
            process = getattr(self, "process", None)
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    def _wait_ready(process) -> None:
        import time
        import urllib.error
        import urllib.request

        deadline = time.monotonic() + 18 * MINUTES
        request = urllib.request.Request(f"http://127.0.0.1:{PORT}/health")
        while time.monotonic() < deadline:
            if (code := process.poll()) is not None:
                raise RuntimeError(f"vllm exited with {code} before becoming healthy")
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            time.sleep(2)
        raise TimeoutError("vllm did not become healthy in 18 minutes")


if __name__ == "__main__":
    raise SystemExit(main())
