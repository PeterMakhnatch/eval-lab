"""Deploy the existing credential-relay role, with optional native OTLP adoption.

This is a source-adoption entrypoint, not authorization to deploy. The historical
campaign relay under runs/ remains frozen and is not a caller of this source.
"""

from __future__ import annotations

import os
from pathlib import Path

import modal

MODEL_ID = "XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
MODEL_SECRET = "evallab-mimo-v26-9b-api-key"

app = modal.App("evallab-mimo-v26-9b-credential-bridge")
_source = Path(__file__).resolve().parents[2] / "src" / "evallab"
image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("fastapi==0.141.1", "opentelemetry-proto==1.44.0", "protobuf==7.35.1")
    .add_local_dir(_source, remote_path="/opt/evallab/evallab", copy=True)
    .env({"PYTHONPATH": "/opt/evallab"})
)
_secrets = [
    modal.Secret.from_name(MODEL_SECRET),
    modal.Secret.from_dict(
        {
            "HAR157_BRIDGE_CAPABILITY": os.environ["HAR157_BRIDGE_CAPABILITY"],
            "HAR157_MODEL_UPSTREAM": os.environ["HAR157_MODEL_UPSTREAM"],
            "EVALLAB_NATIVE_MODEL_SESSIONS": os.environ.get("EVALLAB_NATIVE_MODEL_SESSIONS", "[]"),
        }
    ),
]
if native_secret := os.environ.get("EVALLAB_NATIVE_TELEMETRY_SECRET"):
    # This named secret contains LMNR_PROJECT_API_KEY and the distinct native
    # EVALLAB_NATIVE_TELEMETRY_CAPABILITY, never the sandbox's chat capability.
    _secrets.append(modal.Secret.from_name(native_secret))


@app.function(
    image=image,
    cpu=0.25,
    memory=512,
    timeout=3600,
    min_containers=0,
    max_containers=2,
    scaledown_window=300,
    secrets=_secrets,
)
@modal.asgi_app()
def relay():
    from evallab.credential_bridge import BridgeConfig, create_bridge_app, model_session_bindings

    return create_bridge_app(
        BridgeConfig(
            model_id=MODEL_ID,
            model_upstream=os.environ["HAR157_MODEL_UPSTREAM"],
            model_api_key=os.environ["SGLANG_API_KEY"],
            chat_capability=os.environ["HAR157_BRIDGE_CAPABILITY"],
            telemetry_capability=os.environ.get("EVALLAB_NATIVE_TELEMETRY_CAPABILITY"),
            laminar_api_key=os.environ.get("LMNR_PROJECT_API_KEY"),
            model_sessions=model_session_bindings(os.environ["EVALLAB_NATIVE_MODEL_SESSIONS"]),
        )
    )
