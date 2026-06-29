import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

SCOPE_NAME = "langfuse2instana"
SCOPE_VERSION = "1.0.0"

OTEL_STATUS_OK = "STATUS_CODE_OK"
OTEL_STATUS_ERROR = "STATUS_CODE_ERROR"
OTEL_STATUS_UNSET = "STATUS_CODE_UNSET"

MAX_ATTRIBUTE_LENGTH = 4096


def _str_to_trace_id(s: str) -> str:
    h = hashlib.sha256(s.encode()).hexdigest()
    return h[:32]


def _str_to_span_id(s: str) -> str:
    h = hashlib.sha256(s.encode()).hexdigest()
    return h[:16]


def _iso_to_unix_nano(iso_str: str) -> str:
    if not iso_str:
        return "0"
    iso_str = iso_str.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        try:
            dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%S.%f%z")
        except ValueError:
            dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%S%z")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return str(int(dt.timestamp() * 1_000_000_000))


def _make_kv(key: str, value: Any) -> Optional[dict]:
    if value is None:
        return None
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    if isinstance(value, (list, tuple)):
        return {"key": key, "value": {"stringValue": json.dumps(value)}}
    if isinstance(value, dict):
        return {"key": key, "value": {"stringValue": json.dumps(value)}}
    s = str(value)
    if len(s) > MAX_ATTRIBUTE_LENGTH:
        s = s[:MAX_ATTRIBUTE_LENGTH] + "...[truncated]"
    return {"key": key, "value": {"stringValue": s}}


def _safe_kv(key: str, value: Any) -> list[dict]:
    kv = _make_kv(key, value)
    return [kv] if kv else []


def _first_present(d: dict, keys: tuple) -> Any:
    """Return the first key whose value is not None (0 is a valid value)."""
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


def _observation_to_span(
    obs: dict[str, Any],
    trace_id_hex: str,
    trace_data: dict[str, Any],
    include_io: bool = False,
) -> dict[str, Any]:
    obs_id = obs.get("id", "")
    parent_obs_id = obs.get("parentObservationId")
    obs_type = obs.get("type", "SPAN")
    name = obs.get("name", obs_type)

    span_id_hex = _str_to_span_id(obs_id)
    parent_span_id_hex = _str_to_span_id(parent_obs_id) if parent_obs_id else ""

    start_time = obs.get("startTime", "")
    end_time = obs.get("endTime") or obs.get("completionStartTime") or start_time

    start_nano = _iso_to_unix_nano(start_time)
    end_nano = _iso_to_unix_nano(end_time)

    level = obs.get("level", "DEFAULT")
    if level == "ERROR":
        status_code = OTEL_STATUS_ERROR
    elif level in ("WARNING", "DEBUG"):
        status_code = OTEL_STATUS_UNSET
    else:
        status_code = OTEL_STATUS_OK

    status_message = obs.get("statusMessage", "")

    attributes = []

    attributes.extend(_safe_kv("langfuse.observation.type", obs_type))
    attributes.extend(_safe_kv("langfuse.observation.id", obs_id))
    attributes.extend(_safe_kv("langfuse.trace.id", trace_data.get("id")))
    attributes.extend(_safe_kv("langfuse.trace.name", trace_data.get("name")))

    trace_user_id = trace_data.get("userId")
    if trace_user_id:
        attributes.extend(_safe_kv("enduser.id", trace_user_id))

    trace_session_id = trace_data.get("sessionId")
    if trace_session_id:
        attributes.extend(_safe_kv("session.id", trace_session_id))

    trace_tags = trace_data.get("tags")
    if trace_tags:
        attributes.extend(_safe_kv("langfuse.trace.tags", trace_tags))

    trace_release = trace_data.get("release")
    if trace_release:
        attributes.extend(_safe_kv("langfuse.trace.release", trace_release))

    trace_version = trace_data.get("version")
    if trace_version:
        attributes.extend(_safe_kv("langfuse.trace.version", trace_version))

    obs_version = obs.get("version")
    if obs_version:
        attributes.extend(_safe_kv("langfuse.observation.version", obs_version))

    obs_level = obs.get("level")
    if obs_level:
        attributes.extend(_safe_kv("langfuse.observation.level", obs_level))

    metadata = trace_data.get("metadata")
    if isinstance(metadata, dict):
        for mk, mv in metadata.items():
            attributes.extend(_safe_kv(f"langfuse.trace.metadata.{mk}", mv))

    obs_metadata = obs.get("metadata")
    if isinstance(obs_metadata, dict):
        for mk, mv in obs_metadata.items():
            if mk == "attributes" and isinstance(mv, dict):
                for ak, av in mv.items():
                    attributes.extend(_safe_kv(ak, av))
            else:
                attributes.extend(_safe_kv(f"langfuse.observation.metadata.{mk}", mv))

    if obs_type == "GENERATION":
        attributes.extend(_safe_kv("gen_ai.operation.name", "chat"))
        model = obs.get("model")
        if model:
            attributes.extend(_safe_kv("gen_ai.request.model", model))

        model_params = obs.get("modelParameters")
        if isinstance(model_params, dict):
            param_mapping = {
                "temperature": "gen_ai.request.temperature",
                "max_tokens": "gen_ai.request.max_tokens",
                "maxTokens": "gen_ai.request.max_tokens",
                "top_p": "gen_ai.request.top_p",
                "topP": "gen_ai.request.top_p",
                "frequency_penalty": "gen_ai.request.frequency_penalty",
                "presence_penalty": "gen_ai.request.presence_penalty",
                "stop": "gen_ai.request.stop_sequences",
            }
            for param_key, otel_key in param_mapping.items():
                val = model_params.get(param_key)
                if val is not None:
                    try:
                        val = float(val) if "." in str(val) else int(val)
                    except (ValueError, TypeError):
                        pass
                    attributes.extend(_safe_kv(otel_key, val))

        usage = obs.get("usage") or {}
        if isinstance(usage, dict):
            # Use explicit None checks (not `or`) so a legitimate 0-token count
            # is not skipped in favor of a later field variant.
            input_tokens = _first_present(usage, ("input", "promptTokens", "inputTokens"))
            output_tokens = _first_present(usage, ("output", "completionTokens", "outputTokens"))
            total_tokens = _first_present(usage, ("total", "totalTokens"))

            if input_tokens is not None:
                attributes.extend(_safe_kv("gen_ai.usage.input_tokens", int(input_tokens)))
            if output_tokens is not None:
                attributes.extend(_safe_kv("gen_ai.usage.output_tokens", int(output_tokens)))
            if total_tokens is not None:
                attributes.extend(_safe_kv("gen_ai.usage.total_tokens", int(total_tokens)))

        total_cost = _first_present(obs, ("calculatedTotalCost", "totalCost"))
        if total_cost is not None:
            attributes.extend(_safe_kv("gen_ai.usage.cost", float(total_cost)))

        input_cost = obs.get("calculatedInputCost")
        if input_cost is not None:
            attributes.extend(_safe_kv("gen_ai.usage.input_cost", float(input_cost)))

        output_cost = obs.get("calculatedOutputCost")
        if output_cost is not None:
            attributes.extend(_safe_kv("gen_ai.usage.output_cost", float(output_cost)))

        completion_start = obs.get("completionStartTime")
        if completion_start and start_time:
            ttft_nano = int(_iso_to_unix_nano(completion_start)) - int(_iso_to_unix_nano(start_time))
            if ttft_nano > 0:
                attributes.extend(_safe_kv("gen_ai.response.time_to_first_token_ms", ttft_nano / 1_000_000))

        # Langfuse does not expose a finish reason; only surface "error" when the
        # observation level marks a failure so we don't claim a clean stop.
        if level == "ERROR":
            attributes.extend(_safe_kv("gen_ai.response.finish_reasons", ["error"]))

        if include_io:
            obs_input = obs.get("input")
            if obs_input is not None:
                attributes.extend(_safe_kv("gen_ai.prompt", obs_input))
            obs_output = obs.get("output")
            if obs_output is not None:
                attributes.extend(_safe_kv("gen_ai.completion", obs_output))

    elif include_io:
        obs_input = obs.get("input")
        if obs_input is not None:
            attributes.extend(_safe_kv("langfuse.input", obs_input))
        obs_output = obs.get("output")
        if obs_output is not None:
            attributes.extend(_safe_kv("langfuse.output", obs_output))

    # GENERATION is an outbound call to an LLM provider -> CLIENT so Instana
    # classifies it as an exit/remote call; everything else stays INTERNAL.
    span_kind = "SPAN_KIND_CLIENT" if obs_type == "GENERATION" else "SPAN_KIND_INTERNAL"

    span = {
        "traceId": trace_id_hex,
        "spanId": span_id_hex,
        "name": name,
        "kind": span_kind,
        "startTimeUnixNano": start_nano,
        "endTimeUnixNano": end_nano,
        "attributes": attributes,
        "status": {"code": status_code},
    }

    if parent_span_id_hex:
        span["parentSpanId"] = parent_span_id_hex

    if status_message:
        span["status"]["message"] = status_message

    return span


def convert_trace_to_otlp(
    trace_data: dict[str, Any],
    service_name: str,
    source_name: str,
    environment: Optional[str] = None,
    include_io: bool = False,
) -> Optional[dict[str, Any]]:
    trace_id = trace_data.get("id", "")
    observations = trace_data.get("observations", [])

    if not observations:
        logger.debug("Trace %s has no observations, skipping", trace_id)
        return None

    trace_id_hex = _str_to_trace_id(trace_id)

    resource_attributes = [
        {"key": "service.name", "value": {"stringValue": service_name}},
        {"key": "langfuse.source", "value": {"stringValue": source_name}},
        {"key": "langfuse.trace.id", "value": {"stringValue": trace_id}},
    ]
    if environment:
        resource_attributes.append(
            {"key": "deployment.environment", "value": {"stringValue": environment}}
        )

    obs_ids = {obs.get("id") for obs in observations}

    def _is_top_level(obs: dict) -> bool:
        # Top-level = no parent, OR a parent that isn't in the fetched set
        # (an "orphan" whose parent we never received). Both must hang off the
        # trace root rather than keep a dangling parentSpanId.
        parent = obs.get("parentObservationId")
        return (not parent) or (parent not in obs_ids)

    top_level_ids = {obs.get("id") for obs in observations if _is_top_level(obs)}

    # Keep the trace a single connected tree: only skip the synthetic root when
    # there is exactly one natural top-level span to act as the root.
    need_synthetic_root = len(top_level_ids) != 1
    synthetic_root_span_id = _str_to_span_id(f"root-{trace_id}")

    spans = []
    if need_synthetic_root:
        spans.append(_create_trace_root_span(trace_data, trace_id_hex))

    for obs in observations:
        try:
            span = _observation_to_span(obs, trace_id_hex, trace_data, include_io)
            if obs.get("id") in top_level_ids:
                if need_synthetic_root:
                    span["parentSpanId"] = synthetic_root_span_id
                else:
                    # The single natural root: drop any (dangling) parent ref and
                    # promote it to SERVER so Instana treats it as an entry span.
                    span.pop("parentSpanId", None)
                    if span.get("kind") == "SPAN_KIND_INTERNAL":
                        span["kind"] = "SPAN_KIND_SERVER"
            spans.append(span)
        except Exception as e:
            logger.warning(
                "Failed to convert observation %s in trace %s: %s",
                obs.get("id"), trace_id, e,
            )

    if not spans:
        return None

    return {
        "resourceSpans": [{
            "resource": {"attributes": resource_attributes},
            "scopeSpans": [{
                "scope": {"name": SCOPE_NAME, "version": SCOPE_VERSION},
                "spans": spans,
            }],
        }],
    }


def _create_trace_root_span(
    trace_data: dict[str, Any],
    trace_id_hex: str,
) -> dict[str, Any]:
    trace_id = trace_data.get("id", "")
    name = trace_data.get("name") or f"trace-{trace_id[:8]}"

    observations = trace_data.get("observations", [])
    start_times = [obs.get("startTime") for obs in observations if obs.get("startTime")]
    end_times = [obs.get("endTime") for obs in observations if obs.get("endTime")]

    start_time = min(start_times) if start_times else ""
    end_time = max(end_times) if end_times else ""

    attributes = []
    attributes.extend(_safe_kv("langfuse.trace.id", trace_id))
    attributes.extend(_safe_kv("langfuse.trace.name", name))

    return {
        "traceId": trace_id_hex,
        "spanId": _str_to_span_id(f"root-{trace_id}"),
        "name": name,
        "kind": "SPAN_KIND_SERVER",
        "startTimeUnixNano": _iso_to_unix_nano(start_time),
        "endTimeUnixNano": _iso_to_unix_nano(end_time),
        "attributes": attributes,
        "status": {"code": OTEL_STATUS_OK},
    }


def extract_metrics_from_traces(
    traces: list[dict[str, Any]],
    service_name: str,
    source_name: str,
) -> dict[str, Any]:
    token_counters: dict[str, dict[str, int]] = {}
    cost_counters: dict[str, float] = {}
    duration_values: list[dict[str, Any]] = []

    for trace_data in traces:
        observations = trace_data.get("observations", [])
        for obs in observations:
            if obs.get("type") != "GENERATION":
                continue

            model = obs.get("model", "unknown")

            usage = obs.get("usage") or {}
            if isinstance(usage, dict):
                input_tokens = usage.get("input") or usage.get("promptTokens") or usage.get("inputTokens") or 0
                output_tokens = usage.get("output") or usage.get("completionTokens") or usage.get("outputTokens") or 0

                key = model
                if key not in token_counters:
                    token_counters[key] = {"input": 0, "output": 0}
                token_counters[key]["input"] += int(input_tokens)
                token_counters[key]["output"] += int(output_tokens)

            cost = obs.get("calculatedTotalCost") or obs.get("totalCost")
            if cost is not None:
                cost_counters[model] = cost_counters.get(model, 0.0) + float(cost)

            start_time = obs.get("startTime", "")
            end_time = obs.get("endTime", "")
            if start_time and end_time:
                start_ns = int(_iso_to_unix_nano(start_time))
                end_ns = int(_iso_to_unix_nano(end_time))
                duration_ms = (end_ns - start_ns) / 1_000_000
                if duration_ms > 0:
                    duration_values.append({
                        "model": model,
                        "duration_ms": duration_ms,
                        "name": obs.get("name", ""),
                    })

    return {
        "service_name": service_name,
        "source_name": source_name,
        "token_usage": token_counters,
        "cost": cost_counters,
        "durations": duration_values,
    }


def metrics_to_otlp(metrics_data: dict[str, Any]) -> Optional[dict[str, Any]]:
    service_name = metrics_data["service_name"]
    source_name = metrics_data["source_name"]
    token_usage = metrics_data.get("token_usage", {})
    cost = metrics_data.get("cost", {})
    durations = metrics_data.get("durations", [])

    if not token_usage and not cost and not durations:
        return None

    now_nano = str(int(datetime.now(timezone.utc).timestamp() * 1_000_000_000))

    resource_attributes = [
        {"key": "service.name", "value": {"stringValue": service_name}},
        {"key": "langfuse.source", "value": {"stringValue": source_name}},
    ]

    metrics = []

    for model, counts in token_usage.items():
        for token_type, count in counts.items():
            if count > 0:
                metrics.append({
                    "name": "gen_ai.client.token.usage",
                    "description": "Token usage by model and type",
                    "unit": "token",
                    "sum": {
                        "dataPoints": [{
                            "asInt": str(count),
                            "timeUnixNano": now_nano,
                            "attributes": [
                                {"key": "gen_ai.request.model", "value": {"stringValue": model}},
                                {"key": "gen_ai.token.type", "value": {"stringValue": token_type}},
                            ],
                        }],
                        "aggregationTemporality": "AGGREGATION_TEMPORALITY_DELTA",
                        "isMonotonic": True,
                    },
                })

    for model, total_cost in cost.items():
        if total_cost > 0:
            metrics.append({
                "name": "gen_ai.client.cost",
                "description": "Cost by model",
                "unit": "usd",
                "sum": {
                    "dataPoints": [{
                        "asDouble": total_cost,
                        "timeUnixNano": now_nano,
                        "attributes": [
                            {"key": "gen_ai.request.model", "value": {"stringValue": model}},
                        ],
                    }],
                    "aggregationTemporality": "AGGREGATION_TEMPORALITY_DELTA",
                    "isMonotonic": True,
                },
            })

    # Per-generation latency, emitted as a gauge (one data point per generation).
    duration_points = []
    for d in metrics_data.get("durations", []):
        duration_points.append({
            "asDouble": d["duration_ms"],
            "timeUnixNano": now_nano,
            "attributes": [
                {"key": "gen_ai.request.model", "value": {"stringValue": d.get("model", "unknown")}},
            ],
        })
    if duration_points:
        metrics.append({
            "name": "gen_ai.client.operation.duration",
            "description": "Generation latency",
            "unit": "ms",
            "gauge": {"dataPoints": duration_points},
        })

    if not metrics:
        return None

    return {
        "resourceMetrics": [{
            "resource": {"attributes": resource_attributes},
            "scopeMetrics": [{
                "scope": {"name": SCOPE_NAME, "version": SCOPE_VERSION},
                "metrics": metrics,
            }],
        }],
    }
