import argparse
import asyncio
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import AppConfig, SourceConfig, load_config
from .converter import convert_trace_to_otlp, extract_metrics_from_traces, metrics_to_otlp
from .exporter import OTLPExporter
from .langfuse_client import LangfuseClient
from .state import StateStore

logger = logging.getLogger(__name__)

CLEANUP_INTERVAL_HOURS = 6


class SourcePoller:
    def __init__(
        self,
        source: SourceConfig,
        exporter: OTLPExporter,
        state: StateStore,
        metrics_enabled: bool = True,
    ):
        self.source = source
        self.client = LangfuseClient(source)
        self.exporter = exporter
        self.state = state
        self.metrics_enabled = metrics_enabled
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        logger.info(
            "[%s] Starting poller: interval=%ds, lookback=%dm, host=%s",
            self.source.name,
            self.source.poll_interval_seconds,
            self.source.lookback_minutes,
            self.source.langfuse_host,
        )

        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as e:
                logger.error("[%s] Poll error: %s", self.source.name, e, exc_info=True)

            self._stop.wait(timeout=self.source.poll_interval_seconds)

        logger.info("[%s] Poller stopped", self.source.name)

    def _poll_once(self):
        checkpoint = self.state.get_checkpoint(self.source.name)

        now = datetime.now(timezone.utc)
        # Always re-scan at least the last `lookback_minutes` window so that
        # traces that arrived in Langfuse after their start timestamp (ingestion
        # lag) are still picked up. Deduplication (state store) prevents
        # re-exporting traces we already sent. If the checkpoint is older than
        # the lookback window (e.g. we were down for a while), extend the window
        # back to the checkpoint to catch up.
        lookback_start = now - timedelta(minutes=self.source.lookback_minutes)
        from_dt = lookback_start
        if checkpoint:
            try:
                cp_dt = datetime.fromisoformat(checkpoint.replace("Z", "+00:00"))
                if cp_dt < lookback_start:
                    from_dt = cp_dt
            except ValueError:
                logger.warning("[%s] Invalid checkpoint '%s', using lookback window",
                               self.source.name, checkpoint)

        from_time = from_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        to_time = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        logger.info("[%s] Polling traces from %s to %s", self.source.name, from_time, to_time)

        traces_summary = self.client.fetch_all_traces(
            from_timestamp=from_time,
            to_timestamp=to_time,
            max_traces=self.source.max_traces_per_poll,
        )

        if not traces_summary:
            logger.debug("[%s] No new traces found", self.source.name)
            self.state.set_checkpoint(self.source.name, to_time)
            return

        new_traces = []
        for t in traces_summary:
            trace_id = t.get("id", "")
            if not self.state.is_trace_exported(trace_id, self.source.name):
                new_traces.append(t)

        if not new_traces:
            logger.debug("[%s] All %d traces already exported", self.source.name, len(traces_summary))
            self.state.set_checkpoint(self.source.name, to_time)
            return

        logger.info("[%s] Found %d new traces to export", self.source.name, len(new_traces))

        exported_ids = []
        full_traces = []

        for trace_summary in new_traces:
            trace_id = trace_summary.get("id", "")
            try:
                trace_data = self.client.fetch_trace_with_observations(trace_id)
                full_traces.append(trace_data)

                otlp_data = convert_trace_to_otlp(
                    trace_data=trace_data,
                    service_name=self.source.service_name,
                    source_name=self.source.name,
                    environment=self.source.environment,
                    include_io=self.source.include_io,
                )

                if otlp_data:
                    success = self.exporter.export_traces(otlp_data)
                    if success:
                        exported_ids.append(trace_id)
                        logger.info("[%s] Exported trace %s", self.source.name, trace_id)
                    else:
                        logger.warning("[%s] Failed to export trace %s", self.source.name, trace_id)
                else:
                    exported_ids.append(trace_id)
                    logger.debug("[%s] Trace %s has no spans, marked as exported", self.source.name, trace_id)

            except Exception as e:
                logger.error("[%s] Error processing trace %s: %s", self.source.name, trace_id, e)

        if exported_ids:
            self.state.mark_traces_exported(exported_ids, self.source.name)

        if self.metrics_enabled and full_traces:
            try:
                metrics_data = extract_metrics_from_traces(
                    full_traces, self.source.service_name, self.source.name
                )
                otlp_metrics = metrics_to_otlp(metrics_data)
                if otlp_metrics:
                    self.exporter.export_metrics(otlp_metrics)
                    logger.info("[%s] Exported metrics for %d traces", self.source.name, len(full_traces))
            except Exception as e:
                logger.error("[%s] Error exporting metrics: %s", self.source.name, e)

        self.state.set_checkpoint(self.source.name, to_time)
        logger.info(
            "[%s] Poll complete: %d/%d traces exported",
            self.source.name, len(exported_ids), len(new_traces),
        )

    def process_single_trace(self, trace_id: str) -> bool:
        try:
            trace_data = self.client.fetch_trace_with_observations(trace_id)
            otlp_data = convert_trace_to_otlp(
                trace_data=trace_data,
                service_name=self.source.service_name,
                source_name=self.source.name,
                environment=self.source.environment,
                include_io=self.source.include_io,
            )
            if otlp_data:
                success = self.exporter.export_traces(otlp_data)
                if success:
                    self.state.mark_trace_exported(trace_id, self.source.name)
                    logger.info("[%s] Exported trace %s (webhook)", self.source.name, trace_id)

                    metrics_data = extract_metrics_from_traces(
                        [trace_data], self.source.service_name, self.source.name,
                    )
                    otlp_metrics = metrics_to_otlp(metrics_data)
                    if otlp_metrics:
                        self.exporter.export_metrics(otlp_metrics)

                    return True
            return False
        except Exception as e:
            logger.error("[%s] Error processing trace %s: %s", self.source.name, trace_id, e)
            return False


def main():
    parser = argparse.ArgumentParser(description="Langfuse to Instana/OTLP conversion service")
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Path to configuration file (default: config.yaml)",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not config.sources:
        logger.error("No Langfuse sources configured")
        sys.exit(1)

    if not config.export.endpoint:
        logger.error("No export endpoint configured")
        sys.exit(1)

    exporter = OTLPExporter(config.export)
    state = StateStore(config.state.db_path, config.state.retention_days)

    print("=" * 60)
    print("Langfuse → OTLP/Instana Conversion Service")
    print("=" * 60)
    print(f"Sources: {len(config.sources)}")
    for src in config.sources:
        print(f"  - {src.name}: {src.langfuse_host} (every {src.poll_interval_seconds}s)")
    print(f"Export: {config.export.endpoint} ({config.export.protocol})")
    print(f"Metrics: {'enabled' if config.metrics.enabled else 'disabled'}")
    print(f"Webhook: {'enabled' if config.webhook.enabled else 'disabled'}")
    print("=" * 60)

    pollers: list[SourcePoller] = []
    threads: list[threading.Thread] = []

    source_poller_map: dict[str, SourcePoller] = {}

    for source in config.sources:
        poller = SourcePoller(source, exporter, state, config.metrics.enabled)
        pollers.append(poller)
        source_poller_map[source.name] = poller
        t = threading.Thread(target=poller.run, name=f"poller-{source.name}", daemon=True)
        threads.append(t)

    cleanup_stop = threading.Event()

    def cleanup_loop():
        while not cleanup_stop.is_set():
            cleanup_stop.wait(timeout=CLEANUP_INTERVAL_HOURS * 3600)
            if not cleanup_stop.is_set():
                state.cleanup_old_entries()

    cleanup_thread = threading.Thread(target=cleanup_loop, name="cleanup", daemon=True)

    webhook_thread = None
    if config.webhook.enabled:
        from .webhook import create_webhook_server

        app = create_webhook_server(source_poller_map, config.webhook)

        def run_webhook():
            import uvicorn
            uvicorn.run(
                app,
                host=config.webhook.host,
                port=config.webhook.port,
                log_level=config.log_level.lower(),
            )

        webhook_thread = threading.Thread(target=run_webhook, name="webhook", daemon=True)

    shutdown = threading.Event()

    def signal_handler(signum, frame):
        logger.info("Received signal %d, shutting down...", signum)
        for poller in pollers:
            poller.stop()
        cleanup_stop.set()
        shutdown.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    # Windows service managers (e.g. NSSM) signal stop via CTRL_BREAK_EVENT,
    # which Python delivers as SIGBREAK. Register it when available so the
    # service shuts down gracefully on Windows.
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal_handler)

    for t in threads:
        t.start()
    cleanup_thread.start()
    if webhook_thread:
        webhook_thread.start()

    try:
        shutdown.wait()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt, shutting down...")
        for poller in pollers:
            poller.stop()
        cleanup_stop.set()

    for t in threads:
        t.join(timeout=5)

    print("\nService stopped.")


if __name__ == "__main__":
    main()
