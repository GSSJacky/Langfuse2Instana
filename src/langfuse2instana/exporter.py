import logging
import time
from typing import Any, Optional

import requests

from .config import ExportConfig

logger = logging.getLogger(__name__)

try:
    from opentelemetry.proto.trace.v1.trace_pb2 import TracesData
    from opentelemetry.proto.metrics.v1.metrics_pb2 import MetricsData
    from opentelemetry.proto.resource.v1.resource_pb2 import Resource
    from opentelemetry.proto.common.v1.common_pb2 import (
        KeyValue,
        AnyValue,
        InstrumentationScope,
    )
    PROTOBUF_AVAILABLE = True
except ImportError:
    PROTOBUF_AVAILABLE = False
    logger.warning(
        "opentelemetry-proto not installed. "
        "Only JSON export will be available. "
        "Install with: pip install opentelemetry-proto"
    )


def _hex_to_bytes(hex_string: str) -> bytes:
    hex_string = hex_string.strip()
    if len(hex_string) % 2 != 0:
        hex_string = "0" + hex_string
    return bytes.fromhex(hex_string)


def _set_any_value(av, value_json: dict):
    if "stringValue" in value_json:
        av.string_value = value_json["stringValue"]
    elif "intValue" in value_json:
        av.int_value = int(value_json["intValue"])
    elif "boolValue" in value_json:
        av.bool_value = value_json["boolValue"]
    elif "doubleValue" in value_json:
        av.double_value = float(value_json["doubleValue"])


def _convert_attributes(attrs_json: list[dict], target):
    for attr_json in attrs_json:
        kv = target.add()
        kv.key = attr_json["key"]
        _set_any_value(kv.value, attr_json["value"])


def convert_traces_json_to_protobuf(trace_json: dict[str, Any]) -> bytes:
    if not PROTOBUF_AVAILABLE:
        raise RuntimeError("opentelemetry-proto is required for protobuf export")

    traces_data = TracesData()

    for resource_span_json in trace_json.get("resourceSpans", []):
        resource_span = traces_data.resource_spans.add()

        resource_json = resource_span_json.get("resource", {})
        _convert_attributes(resource_json.get("attributes", []), resource_span.resource.attributes)

        for scope_span_json in resource_span_json.get("scopeSpans", []):
            scope_span = resource_span.scope_spans.add()

            scope_json = scope_span_json.get("scope", {})
            scope_span.scope.name = scope_json.get("name", "")
            scope_span.scope.version = scope_json.get("version", "")

            for span_json in scope_span_json.get("spans", []):
                span = scope_span.spans.add()

                span.trace_id = _hex_to_bytes(span_json["traceId"])
                span.span_id = _hex_to_bytes(span_json["spanId"])

                if span_json.get("parentSpanId"):
                    span.parent_span_id = _hex_to_bytes(span_json["parentSpanId"])

                span.name = span_json.get("name", "")

                kind_map = {
                    "SPAN_KIND_UNSPECIFIED": 0,
                    "SPAN_KIND_INTERNAL": 1,
                    "SPAN_KIND_SERVER": 2,
                    "SPAN_KIND_CLIENT": 3,
                    "SPAN_KIND_PRODUCER": 4,
                    "SPAN_KIND_CONSUMER": 5,
                }
                span.kind = kind_map.get(span_json.get("kind", "SPAN_KIND_INTERNAL"), 1)

                span.start_time_unix_nano = int(span_json.get("startTimeUnixNano", "0"))
                span.end_time_unix_nano = int(span_json.get("endTimeUnixNano", "0"))

                _convert_attributes(span_json.get("attributes", []), span.attributes)

                for link_json in span_json.get("links", []):
                    link = span.links.add()
                    link.trace_id = _hex_to_bytes(link_json["traceId"])
                    link.span_id = _hex_to_bytes(link_json["spanId"])
                    _convert_attributes(link_json.get("attributes", []), link.attributes)

                status_json = span_json.get("status", {})
                status_code_map = {
                    "STATUS_CODE_UNSET": 0,
                    "STATUS_CODE_OK": 1,
                    "STATUS_CODE_ERROR": 2,
                }
                span.status.code = status_code_map.get(
                    status_json.get("code", "STATUS_CODE_UNSET"), 0
                )
                if "message" in status_json:
                    span.status.message = status_json["message"]

    return traces_data.SerializeToString()


def convert_metrics_json_to_protobuf(metrics_json: dict[str, Any]) -> bytes:
    if not PROTOBUF_AVAILABLE:
        raise RuntimeError("opentelemetry-proto is required for protobuf export")

    metrics_data = MetricsData()

    for resource_metrics_json in metrics_json.get("resourceMetrics", []):
        resource_metrics = metrics_data.resource_metrics.add()

        resource_json = resource_metrics_json.get("resource", {})
        _convert_attributes(resource_json.get("attributes", []), resource_metrics.resource.attributes)

        for scope_metrics_json in resource_metrics_json.get("scopeMetrics", []):
            scope_metrics = resource_metrics.scope_metrics.add()

            scope_json = scope_metrics_json.get("scope", {})
            scope_metrics.scope.name = scope_json.get("name", "")
            scope_metrics.scope.version = scope_json.get("version", "")

            for metric_json in scope_metrics_json.get("metrics", []):
                metric = scope_metrics.metrics.add()
                metric.name = metric_json.get("name", "")
                metric.description = metric_json.get("description", "")
                metric.unit = metric_json.get("unit", "")

                if "sum" in metric_json:
                    sum_json = metric_json["sum"]
                    agg_temp_map = {
                        "AGGREGATION_TEMPORALITY_UNSPECIFIED": 0,
                        "AGGREGATION_TEMPORALITY_DELTA": 1,
                        "AGGREGATION_TEMPORALITY_CUMULATIVE": 2,
                    }
                    metric.sum.aggregation_temporality = agg_temp_map.get(
                        sum_json.get("aggregationTemporality", ""), 1
                    )
                    metric.sum.is_monotonic = sum_json.get("isMonotonic", False)

                    for dp_json in sum_json.get("dataPoints", []):
                        dp = metric.sum.data_points.add()
                        dp.time_unix_nano = int(dp_json.get("timeUnixNano", "0"))
                        if "asInt" in dp_json:
                            dp.as_int = int(dp_json["asInt"])
                        elif "asDouble" in dp_json:
                            dp.as_double = float(dp_json["asDouble"])
                        _convert_attributes(dp_json.get("attributes", []), dp.attributes)

                elif "gauge" in metric_json:
                    gauge_json = metric_json["gauge"]
                    for dp_json in gauge_json.get("dataPoints", []):
                        dp = metric.gauge.data_points.add()
                        dp.time_unix_nano = int(dp_json.get("timeUnixNano", "0"))
                        if "asInt" in dp_json:
                            dp.as_int = int(dp_json["asInt"])
                        elif "asDouble" in dp_json:
                            dp.as_double = float(dp_json["asDouble"])
                        _convert_attributes(dp_json.get("attributes", []), dp.attributes)

    return metrics_data.SerializeToString()


class OTLPExporter:
    def __init__(self, config: ExportConfig):
        self.config = config
        self.endpoint = config.endpoint.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(config.headers)

    def export_traces(self, otlp_json: dict[str, Any]) -> bool:
        if self.config.protocol == "http/protobuf":
            return self._export_traces_protobuf(otlp_json)
        return self._export_traces_json(otlp_json)

    def export_metrics(self, otlp_json: dict[str, Any]) -> bool:
        if self.config.protocol == "http/protobuf":
            return self._export_metrics_protobuf(otlp_json)
        return self._export_metrics_json(otlp_json)

    def _export_traces_protobuf(self, otlp_json: dict[str, Any]) -> bool:
        try:
            data = convert_traces_json_to_protobuf(otlp_json)
        except Exception as e:
            logger.error("Failed to convert traces to protobuf: %s", e)
            return False

        url = f"{self.endpoint}/v1/traces"
        headers = {"Content-Type": "application/x-protobuf"}
        return self._send(url, data, headers)

    def _export_traces_json(self, otlp_json: dict[str, Any]) -> bool:
        url = f"{self.endpoint}/v1/traces"
        headers = {"Content-Type": "application/json"}
        import json
        data = json.dumps(otlp_json).encode("utf-8")
        return self._send(url, data, headers)

    def _export_metrics_protobuf(self, otlp_json: dict[str, Any]) -> bool:
        try:
            data = convert_metrics_json_to_protobuf(otlp_json)
        except Exception as e:
            logger.error("Failed to convert metrics to protobuf: %s", e)
            return False

        url = f"{self.endpoint}/v1/metrics"
        headers = {"Content-Type": "application/x-protobuf"}
        return self._send(url, data, headers)

    def _export_metrics_json(self, otlp_json: dict[str, Any]) -> bool:
        url = f"{self.endpoint}/v1/metrics"
        headers = {"Content-Type": "application/json"}
        import json
        data = json.dumps(otlp_json).encode("utf-8")
        return self._send(url, data, headers)

    def _send(self, url: str, data: bytes, extra_headers: dict) -> bool:
        for attempt in range(self.config.max_retries):
            try:
                resp = self.session.post(
                    url,
                    data=data,
                    headers=extra_headers,
                    timeout=self.config.timeout_seconds,
                    verify=not self.config.insecure,
                )

                if resp.status_code in (200, 202, 204):
                    logger.debug("Successfully exported to %s (status %d)", url, resp.status_code)
                    return True

                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 2 ** attempt))
                    logger.warning("Rate limited by OTLP endpoint, retrying after %ds", retry_after)
                    time.sleep(retry_after)
                    continue

                if resp.status_code >= 500:
                    wait = 2 ** attempt
                    logger.warning(
                        "OTLP endpoint returned %d, retrying after %ds",
                        resp.status_code, wait,
                    )
                    time.sleep(wait)
                    continue

                logger.error(
                    "OTLP endpoint returned %d: %s", resp.status_code, resp.text[:500]
                )
                return False

            except requests.exceptions.RequestException as e:
                wait = 2 ** attempt
                logger.warning("Export request failed (attempt %d/%d): %s", attempt + 1, self.config.max_retries, e)
                if attempt < self.config.max_retries - 1:
                    time.sleep(wait)
                else:
                    logger.error("Export failed after %d retries", self.config.max_retries)
                    return False

        return False
