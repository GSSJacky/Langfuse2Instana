import os
import re
import logging
from dataclasses import dataclass, field
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _resolve_env_vars(value):
    if not isinstance(value, str):
        return value
    def replacer(match):
        var_name = match.group(1)
        env_val = os.environ.get(var_name)
        if env_val is None:
            logger.warning("Environment variable %s is not set", var_name)
            return match.group(0)
        return env_val
    return ENV_VAR_PATTERN.sub(replacer, value)


def _resolve_dict(d):
    if isinstance(d, dict):
        return {k: _resolve_dict(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_resolve_dict(item) for item in d]
    return _resolve_env_vars(d)


@dataclass
class SourceConfig:
    name: str
    langfuse_host: str
    public_key: str
    secret_key: str
    poll_interval_seconds: int = 60
    lookback_minutes: int = 5
    service_name: str = "langfuse-app"
    environment: Optional[str] = None
    max_traces_per_poll: int = 100
    max_observations_per_poll: int = 1000
    include_io: bool = False
    project_id: Optional[str] = None


@dataclass
class ExportConfig:
    endpoint: str
    protocol: str = "http/protobuf"
    headers: dict = field(default_factory=dict)
    insecure: bool = False
    timeout_seconds: int = 30
    max_retries: int = 3


@dataclass
class WebhookConfig:
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    secret: Optional[str] = None


@dataclass
class StateConfig:
    db_path: str = "./state.db"
    retention_days: int = 7


@dataclass
class MetricsConfig:
    enabled: bool = True
    export_interval_seconds: int = 60


@dataclass
class AppConfig:
    sources: list[SourceConfig] = field(default_factory=list)
    export: ExportConfig = field(default_factory=lambda: ExportConfig(endpoint=""))
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    state: StateConfig = field(default_factory=StateConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    log_level: str = "INFO"


def load_config(config_path: str) -> AppConfig:
    with open(config_path, "r") as f:
        raw = yaml.safe_load(f)

    raw = _resolve_dict(raw)

    sources = []
    for src in raw.get("sources", []):
        sources.append(SourceConfig(
            name=src["name"],
            langfuse_host=src["langfuse_host"].rstrip("/"),
            public_key=src["public_key"],
            secret_key=src["secret_key"],
            poll_interval_seconds=src.get("poll_interval_seconds", 60),
            lookback_minutes=src.get("lookback_minutes", 5),
            service_name=src.get("service_name", "langfuse-app"),
            environment=src.get("environment"),
            max_traces_per_poll=src.get("max_traces_per_poll", 100),
            # Observation-based polling cap; default to the trace cap so existing
            # configs that only set max_traces_per_poll keep an equivalent bound.
            max_observations_per_poll=src.get(
                "max_observations_per_poll", src.get("max_traces_per_poll", 1000)
            ),
            include_io=src.get("include_io", False),
            project_id=src.get("project_id"),
        ))

    export_raw = raw.get("export", {})
    export_cfg = ExportConfig(
        endpoint=export_raw.get("endpoint", ""),
        protocol=export_raw.get("protocol", "http/protobuf"),
        headers=export_raw.get("headers", {}),
        insecure=export_raw.get("insecure", False),
        timeout_seconds=export_raw.get("timeout_seconds", 30),
        max_retries=export_raw.get("max_retries", 3),
    )

    webhook_raw = raw.get("webhook", {})
    webhook_cfg = WebhookConfig(
        enabled=webhook_raw.get("enabled", False),
        host=webhook_raw.get("host", "0.0.0.0"),
        port=webhook_raw.get("port", 8000),
        secret=webhook_raw.get("secret"),
    )

    state_raw = raw.get("state", {})
    state_cfg = StateConfig(
        db_path=state_raw.get("db_path", "./state.db"),
        retention_days=state_raw.get("retention_days", 7),
    )

    metrics_raw = raw.get("metrics", {})
    metrics_cfg = MetricsConfig(
        enabled=metrics_raw.get("enabled", True),
        export_interval_seconds=metrics_raw.get("export_interval_seconds", 60),
    )

    return AppConfig(
        sources=sources,
        export=export_cfg,
        webhook=webhook_cfg,
        state=state_cfg,
        metrics=metrics_cfg,
        log_level=raw.get("log_level", "INFO"),
    )
