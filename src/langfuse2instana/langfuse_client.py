import logging
import time
from typing import Any, Optional

import requests

from .config import SourceConfig

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 50
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2


class LangfuseClient:
    def __init__(self, source: SourceConfig):
        self.source = source
        self.base_url = f"{source.langfuse_host}/api/public"
        self.auth = (source.public_key, source.secret_key)
        self.session = requests.Session()
        self.session.auth = self.auth
        self.session.headers.update({"Accept": "application/json"})

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        for attempt in range(MAX_RETRIES):
            try:
                resp = self.session.request(method, url, timeout=30, **kwargs)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", RETRY_BACKOFF_BASE ** attempt))
                    logger.warning("Rate limited, retrying after %ds", retry_after)
                    time.sleep(retry_after)
                    continue
                if resp.status_code >= 500:
                    wait = RETRY_BACKOFF_BASE ** attempt
                    logger.warning("Server error %d, retrying after %ds", resp.status_code, wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp
            except requests.exceptions.ConnectionError as e:
                wait = RETRY_BACKOFF_BASE ** attempt
                logger.warning("Connection error (attempt %d/%d): %s", attempt + 1, MAX_RETRIES, e)
                if attempt < MAX_RETRIES - 1:
                    time.sleep(wait)
                else:
                    raise
        raise RuntimeError(f"Max retries exceeded for {url}")

    def list_traces(
        self,
        from_timestamp: Optional[str] = None,
        to_timestamp: Optional[str] = None,
        limit: int = DEFAULT_PAGE_SIZE,
        page: int = 1,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "page": page}
        if from_timestamp:
            params["fromTimestamp"] = from_timestamp
        if to_timestamp:
            params["toTimestamp"] = to_timestamp
        if name:
            params["name"] = name
        if user_id:
            params["userId"] = user_id
        if session_id:
            params["sessionId"] = session_id
        if tags:
            params["tags"] = tags

        resp = self._request("GET", "/traces", params=params)
        return resp.json()

    def get_trace(self, trace_id: str) -> dict[str, Any]:
        resp = self._request("GET", f"/traces/{trace_id}")
        return resp.json()

    def list_observations(
        self,
        trace_id: Optional[str] = None,
        limit: int = DEFAULT_PAGE_SIZE,
        page: int = 1,
        observation_type: Optional[str] = None,
        name: Optional[str] = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit, "page": page}
        if trace_id:
            params["traceId"] = trace_id
        if observation_type:
            params["type"] = observation_type
        if name:
            params["name"] = name

        resp = self._request("GET", "/observations", params=params)
        return resp.json()

    def get_observation(self, observation_id: str) -> dict[str, Any]:
        resp = self._request("GET", f"/observations/{observation_id}")
        return resp.json()

    def fetch_all_traces(
        self,
        from_timestamp: str,
        to_timestamp: str,
        max_traces: int = 100,
    ) -> list[dict[str, Any]]:
        all_traces = []
        page = 1
        page_size = min(max_traces, DEFAULT_PAGE_SIZE)

        while len(all_traces) < max_traces:
            result = self.list_traces(
                from_timestamp=from_timestamp,
                to_timestamp=to_timestamp,
                limit=page_size,
                page=page,
            )
            traces = result.get("data", [])
            if not traces:
                break
            all_traces.extend(traces)
            if len(traces) < page_size:
                break
            page += 1

        logger.info(
            "[%s] Fetched %d traces from %s to %s",
            self.source.name, len(all_traces), from_timestamp, to_timestamp,
        )
        return all_traces[:max_traces]

    def fetch_trace_with_observations(self, trace_id: str) -> dict[str, Any]:
        trace = self.get_trace(trace_id)
        observations = trace.get("observations", [])
        if not observations:
            all_obs = []
            page = 1
            while True:
                result = self.list_observations(trace_id=trace_id, limit=100, page=page)
                obs = result.get("data", [])
                if not obs:
                    break
                all_obs.extend(obs)
                if len(obs) < 100:
                    break
                page += 1
            trace["observations"] = all_obs
        return trace
