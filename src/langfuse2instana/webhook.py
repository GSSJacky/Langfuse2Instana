import base64
import hashlib
import hmac
import logging
import time
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

SVIX_TOLERANCE_SECONDS = 5 * 60
TRACE_EVENTS = ("trace.created", "trace.updated", "observation.created")


def _verify_svix(secret: str, body: bytes, svix_id: str, svix_ts: str, svix_sig: str) -> bool:
    # Reject replays / clock-skewed deliveries outside the tolerance window.
    try:
        ts = int(svix_ts)
    except ValueError:
        return False
    if abs(time.time() - ts) > SVIX_TOLERANCE_SECONDS:
        return False

    key = secret[len("whsec_"):] if secret.startswith("whsec_") else secret
    try:
        secret_bytes = base64.b64decode(key)
    except Exception:
        secret_bytes = secret.encode()

    signed = f"{svix_id}.{svix_ts}.".encode() + body
    expected = base64.b64encode(
        hmac.new(secret_bytes, signed, hashlib.sha256).digest()
    ).decode()

    # svix-signature is a space-separated list of "v1,<sig>" entries.
    for part in svix_sig.split():
        sig = part.split(",", 1)[1] if "," in part else part
        if hmac.compare_digest(expected, sig):
            return True
    return False


def _verify_signature(secret: Optional[str], body: bytes, headers) -> bool:
    """Return True if the request is authentic (or verification is disabled)."""
    if not secret:
        return True

    # Langfuse webhooks use Svix-style signing.
    svix_id = headers.get("svix-id")
    svix_ts = headers.get("svix-timestamp")
    svix_sig = headers.get("svix-signature")
    if svix_id and svix_ts and svix_sig:
        return _verify_svix(secret, body, svix_id, svix_ts, svix_sig)

    # Fallback: simple raw HMAC-SHA256 hex (custom integrations / /api/trigger).
    provided = headers.get("x-langfuse-signature")
    if provided:
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, provided)

    # A secret is configured but the request carries no signature -> reject.
    return False


def _extract_trace_id(payload: dict) -> Optional[str]:
    data = payload.get("data") or {}
    return data.get("traceId") or data.get("id") or payload.get("traceId")


def create_webhook_server(
    source_poller_map: dict[str, Any],
    webhook_config: Any,
) -> FastAPI:
    app = FastAPI(title="Langfuse2Instana Webhook Receiver")
    webhook_secret = webhook_config.secret

    @app.get("/health")
    async def health():
        return {"status": "ok", "sources": list(source_poller_map.keys())}

    @app.post("/webhook/langfuse")
    async def langfuse_webhook(request: Request):
        body = await request.body()
        if not _verify_signature(webhook_secret, body, request.headers):
            raise HTTPException(status_code=401, detail="Invalid signature")

        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

        event_type = payload.get("event", "")
        logger.info("Received webhook event: %s", event_type)

        if event_type not in TRACE_EVENTS:
            return JSONResponse({"status": "ignored", "event": event_type})

        trace_id = _extract_trace_id(payload)
        if not trace_id:
            return JSONResponse({"status": "ignored", "reason": "no trace_id"})

        source_name = _resolve_source(payload, source_poller_map)
        if not source_name:
            return JSONResponse({"status": "ignored", "reason": "no matching source"})

        poller = source_poller_map[source_name]
        success = poller.process_single_trace(trace_id)
        return JSONResponse({
            "status": "exported" if success else "failed",
            "trace_id": trace_id,
            "source": source_name,
        })

    @app.post("/webhook/langfuse/{source_name}")
    async def langfuse_webhook_named(source_name: str, request: Request):
        body = await request.body()
        if not _verify_signature(webhook_secret, body, request.headers):
            raise HTTPException(status_code=401, detail="Invalid signature")

        poller = source_poller_map.get(source_name)
        if not poller:
            raise HTTPException(status_code=404, detail=f"Source '{source_name}' not found")

        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

        event_type = payload.get("event", "")
        if event_type not in TRACE_EVENTS:
            return JSONResponse({"status": "ignored", "event": event_type})

        trace_id = _extract_trace_id(payload)
        if not trace_id:
            return JSONResponse({"status": "ignored", "reason": "no trace_id"})

        success = poller.process_single_trace(trace_id)
        return JSONResponse({
            "status": "exported" if success else "failed",
            "trace_id": trace_id,
            "source": source_name,
        })

    @app.post("/api/trigger")
    async def trigger_export(request: Request):
        body = await request.body()
        if not _verify_signature(webhook_secret, body, request.headers):
            raise HTTPException(status_code=401, detail="Invalid signature")

        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

        trace_id = payload.get("trace_id")
        source_name = payload.get("source")

        if not trace_id:
            raise HTTPException(status_code=400, detail="trace_id is required")

        if source_name:
            poller = source_poller_map.get(source_name)
            if not poller:
                raise HTTPException(status_code=404, detail=f"Source '{source_name}' not found")
            success = poller.process_single_trace(trace_id)
            return JSONResponse({
                "status": "exported" if success else "failed",
                "trace_id": trace_id,
                "source": source_name,
            })

        for name, poller in source_poller_map.items():
            if poller.process_single_trace(trace_id):
                return JSONResponse({"status": "exported", "trace_id": trace_id, "source": name})

        return JSONResponse({"status": "failed", "trace_id": trace_id})

    return app


def _resolve_source(payload: dict, source_map: dict) -> Optional[str]:
    project_id = (payload.get("data") or {}).get("projectId") or payload.get("projectId")
    if project_id:
        for name, poller in source_map.items():
            if getattr(poller.source, "project_id", None) == project_id:
                return name

    if len(source_map) == 1:
        return next(iter(source_map))

    # Ambiguous: multiple sources and no project match. Require the caller to use
    # the /webhook/langfuse/{source_name} endpoint rather than guessing wrong.
    logger.warning("Cannot resolve source for webhook (project_id=%s); use the named endpoint", project_id)
    return None
