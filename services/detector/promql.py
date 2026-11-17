"""Minimal Prometheus HTTP API client. Only what the detector needs."""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests


@dataclass
class Sample:
    ts: float
    value: float


class PrometheusClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()

    def _get(self, path: str, params: dict) -> dict:
        r = self._session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
        r.raise_for_status()
        payload = r.json()
        if payload.get("status") != "success":
            raise RuntimeError(f"prometheus error: {payload}")
        return payload["data"]

    def instant(self, query: str) -> float | None:
        data = self._get("/api/v1/query", {"query": query})
        result = data.get("result") or []
        if not result:
            return None
        try:
            value = float(result[0]["value"][1])
        except (KeyError, IndexError, ValueError):
            return None
        return None if value != value else value  # drop NaN

    def range(self, query: str, minutes: int, step: str = "15s") -> list[Sample]:
        end = time.time()
        data = self._get(
            "/api/v1/query_range",
            {"query": query, "start": end - minutes * 60, "end": end, "step": step},
        )
        result = data.get("result") or []
        if not result:
            return []
        out: list[Sample] = []
        for ts, raw in result[0].get("values", []):
            try:
                v = float(raw)
            except ValueError:
                continue
            if v == v:
                out.append(Sample(float(ts), v))
        return out

    def ready(self) -> bool:
        try:
            self._session.get(f"{self.base_url}/-/ready", timeout=3).raise_for_status()
            return True
        except Exception:
            return False
