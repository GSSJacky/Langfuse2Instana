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
        # Poll by *observation* start time (not trace start time). WxO maps a
        # whole conversation session to one long-lived trace and appends each
        # turn as new observations over hours; a trace-list poll filtered by
        # trace start time would drop those appended turns once the trace fell
        # out of the lookback window. Always re-scan at least the last
        # `lookback_minutes` to absorb ingestion lag; observation-level dedup
        # prevents re-exporting what we already sent. If the checkpoint is older
        # than the lookback window (e.g. downtime), extend back to it to catch up.
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

        logger.info("[%s] Polling observations from %s to %s", self.source.name, from_time, to_time)

        observations = self.client.fetch_recent_observations(
            from_start_time=from_time,
            to_start_time=to_time,
            max_observations=self.source.max_observations_per_poll,
        )

        if not observations:
            logger.debug("[%s] No observations in window", self.source.name)
            self.state.set_checkpoint(self.source.name, to_time)
            return

        obs_ids = [o.get("id", "") for o in observations]
        new_obs_ids = self.state.filter_new_observation_ids(obs_ids, self.source.name)

        if not new_obs_ids:
            logger.debug("[%s] All %d observations already exported", self.source.name, len(observations))
            self.state.set_checkpoint(self.source.name, to_time)
            return

        # Distinct parent traces that gained new observations -> (re-)export.
        affected_trace_ids = []
        seen = set()
        for o in observations:
            if o.get("id") in new_obs_ids:
                tid = o.get("traceId")
                if tid and tid not in seen:
                    seen.add(tid)
                    affected_trace_ids.append(tid)

        logger.info(
            "[%s] Found %d new observations across %d traces to export",
            self.source.name, len(new_obs_ids), len(affected_trace_ids),
        )

        exported = 0
        for trace_id in affected_trace_ids:
            try:
                trace_data = self.client.fetch_trace_with_observations(trace_id)
                if self._export_trace(trace_data):
                    exported += 1
            except Exception as e:
                logger.error("[%s] Error processing trace %s: %s", self.source.name, trace_id, e)

        self.state.set_checkpoint(self.source.name, to_time)
        logger.info(
            "[%s] Poll complete: %d/%d traces (re-)exported",
            self.source.name, exported, len(affected_trace_ids),
        )

    def _export_trace(self, trace_data: dict) -> bool:
        """Convert and export a full trace, then export metrics for and record
        only its *new* observations.

        Re-exporting the full trace is idempotent for spans (span IDs are a
        deterministic hash of the observation ID, so Instana de-dupes by
        traceId+spanId). Metrics, however, are delta sums, so they are computed
        from new observations only to avoid double counting on re-export.
        """
        trace_id = trace_data.get("id", "")
        all_obs = trace_data.get("observations", []) or []
        all_obs_ids = [o.get("id", "") for o in all_obs if o.get("id")]

        # Determine which observations are genuinely new before we mark anything.
        new_ids = self.state.filter_new_observation_ids(all_obs_ids, self.source.name)

        otlp_data = convert_trace_to_otlp(
            trace_data=trace_data,
            service_name=self.source.service_name,
            source_name=self.source.name,
            environment=self.source.environment,
            include_io=self.source.include_io,
        )

        if otlp_data:
            if not self.exporter.export_traces(otlp_data):
                logger.warning("[%s] Failed to export trace %s", self.source.name, trace_id)
                return False  # not marked -> retried next poll
            logger.info("[%s] Exported trace %s (%d new obs)", self.source.name, trace_id, len(new_ids))
        else:
            logger.debug("[%s] Trace %s has no observations yet", self.source.name, trace_id)

        if self.metrics_enabled and new_ids:
            try:
                new_trace_view = dict(trace_data)
                new_trace_view["observations"] = [o for o in all_obs if o.get("id") in new_ids]
                metrics_data = extract_metrics_from_traces(
                    [new_trace_view], self.source.service_name, self.source.name
                )
                otlp_metrics = metrics_to_otlp(metrics_data)
                if otlp_metrics:
                    self.exporter.export_metrics(otlp_metrics)
                    logger.info("[%s] Exported metrics for trace %s", self.source.name, trace_id)
            except Exception as e:
                logger.error("[%s] Error exporting metrics for trace %s: %s", self.source.name, trace_id, e)

        # Record all of the trace's observations so unchanged ones don't trigger
        # re-export; the next genuinely new observation (next turn) won't be in
        # this set and will trigger another (idempotent) export.
        if all_obs_ids:
            self.state.mark_observations_exported(all_obs_ids, self.source.name)

        return True

    def process_single_trace(self, trace_id: str) -> bool:
        try:
            trace_data = self.client.fetch_trace_with_observations(trace_id)
            return self._export_trace(trace_data)
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
