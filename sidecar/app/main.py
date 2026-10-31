import asyncio
import logging
import os
import re
import time
from typing import Final

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
)
from prometheus_client.parser import text_string_to_metric_families
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("arika.metrics")

NGINX_STATUS_URL: Final = os.getenv(
    "NGINX_STATUS_URL", "http://nginx/nginx_status"
)
NEST_METRICS_URL: Final = os.getenv(
    "NEST_METRICS_URL", "http://nginx/metrics"
)
NGINX_HOST_HEADER: Final = os.getenv("NGINX_HOST_HEADER", "")
HTTP_TIMEOUT_SECONDS: Final = float(os.getenv("HTTP_TIMEOUT_SECONDS", "5"))

REGISTRY = CollectorRegistry()
ProcessCollector(namespace="python", registry=REGISTRY)
PlatformCollector(registry=REGISTRY)

HTTP_REQUESTS = Counter(
    "python_http_requests_total",
    "Total HTTP requests handled by the Python sidecar.",
    ["method", "route", "status_code"],
    registry=REGISTRY,
)
HTTP_REQUEST_DURATION = Histogram(
    "python_http_request_duration_seconds",
    "Duration of HTTP requests handled by the Python sidecar.",
    ["method", "route"],
    registry=REGISTRY,
)
RAG_QUERIES = Counter(
    "rag_queries_total",
    "Total RAG queries submitted to the sidecar.",
    registry=REGISTRY,
)
RAG_QUERY_DURATION = Histogram(
    "rag_query_latency_seconds",
    "Time spent handling a RAG query.",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    registry=REGISTRY,
)

NGINX_ACTIVE_CONNECTIONS = Gauge(
    "nginx_connections_active",
    "Current active connections reported by Nginx stub_status.",
    registry=REGISTRY,
)
NGINX_READING_CONNECTIONS = Gauge(
    "nginx_connections_reading",
    "Current connections reading requests in Nginx.",
    registry=REGISTRY,
)
NGINX_WRITING_CONNECTIONS = Gauge(
    "nginx_connections_writing",
    "Current connections writing responses in Nginx.",
    registry=REGISTRY,
)
NGINX_WAITING_CONNECTIONS = Gauge(
    "nginx_connections_waiting",
    "Current idle keep-alive connections in Nginx.",
    registry=REGISTRY,
)
NGINX_ACCEPTED_CONNECTIONS = Counter(
    "nginx_connections_accepted",
    "Connections accepted by Nginx since startup.",
    registry=REGISTRY,
)
NGINX_HANDLED_CONNECTIONS = Counter(
    "nginx_connections_handled",
    "Connections handled by Nginx since startup.",
    registry=REGISTRY,
)
NGINX_HTTP_REQUESTS = Counter(
    "nginx_http_requests",
    "HTTP requests handled by Nginx since startup.",
    registry=REGISTRY,
)
NGINX_SCRAPE_SUCCESS = Gauge(
    "nginx_scrape_success",
    "Whether the latest Nginx status scrape succeeded.",
    registry=REGISTRY,
)
NEST_SCRAPE_SUCCESS = Gauge(
    "nest_metrics_scrape_success",
    "Whether the latest NestJS metrics scrape succeeded.",
    registry=REGISTRY,
)
NGINX_SCRAPE_ERRORS = Counter(
    "nginx_scrape_errors",
    "Number of failed Nginx status scrapes.",
    registry=REGISTRY,
)
NEST_SCRAPE_ERRORS = Counter(
    "nest_metrics_scrape_errors",
    "Number of failed NestJS metrics scrapes.",
    registry=REGISTRY,
)
NGINX_SCRAPE_DURATION = Histogram(
    "nginx_scrape_duration_seconds",
    "Duration of Nginx status scrapes.",
    registry=REGISTRY,
)
NEST_SCRAPE_DURATION = Histogram(
    "nest_metrics_scrape_duration_seconds",
    "Duration of NestJS metrics scrapes.",
    registry=REGISTRY,
)

_STATUS_LINE = re.compile(r"Active connections:\s+(\d+)")
_CONNECTION_LINE = re.compile(r"(\d+)\s+(\d+)\s+(\d+)")
_STATE_LINE = re.compile(
    r"Reading:\s+(\d+)\s+Writing:\s+(\d+)\s+Waiting:\s+(\d+)"
)
_last_nginx_counters: dict[str, int | None] = {
    "accepted": None,
    "handled": None,
    "requests": None,
}
_scrape_lock = asyncio.Lock()


class AskRagRequest(BaseModel):
    question: str = Field(min_length=1, max_length=10_000)


def _update_counter(counter: Counter, current_value: int, key: str) -> None:
    previous_value = _last_nginx_counters[key]
    increment = (
        current_value
        if previous_value is None or current_value < previous_value
        else current_value - previous_value
    )
    if increment:
        counter.inc(increment)
    _last_nginx_counters[key] = current_value


def _update_nginx_metrics(status_text: str) -> None:
    lines = [line.strip() for line in status_text.splitlines() if line.strip()]
    if len(lines) < 4:
        raise ValueError("Nginx stub_status response has an invalid shape")

    active_match = _STATUS_LINE.fullmatch(lines[0])
    connection_match = _CONNECTION_LINE.fullmatch(lines[2])
    state_match = _STATE_LINE.fullmatch(lines[3])
    if not active_match or not connection_match or not state_match:
        raise ValueError("Nginx stub_status response could not be parsed")

    accepted, handled, requests = map(int, connection_match.groups())
    reading, writing, waiting = map(int, state_match.groups())

    NGINX_ACTIVE_CONNECTIONS.set(int(active_match.group(1)))
    NGINX_READING_CONNECTIONS.set(reading)
    NGINX_WRITING_CONNECTIONS.set(writing)
    NGINX_WAITING_CONNECTIONS.set(waiting)
    _update_counter(NGINX_ACCEPTED_CONNECTIONS, accepted, "accepted")
    _update_counter(NGINX_HANDLED_CONNECTIONS, handled, "handled")
    _update_counter(NGINX_HTTP_REQUESTS, requests, "requests")


app = FastAPI(title="Arika observability sidecar")


@app.middleware("http")
async def record_http_metrics(request: Request, call_next):
    started_at = time.perf_counter()
    status_code = "500"

    try:
        response = await call_next(request)
        status_code = str(response.status_code)
        return response
    finally:
        route = request.scope.get("route")
        route_path = getattr(route, "path", "unmatched")
        HTTP_REQUESTS.labels(
            request.method, route_path, status_code
        ).inc()
        HTTP_REQUEST_DURATION.labels(request.method, route_path).observe(
            time.perf_counter() - started_at
        )


@app.get("/healthz")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> Response:
    nest_metrics = ""
    headers = {"Host": NGINX_HOST_HEADER} if NGINX_HOST_HEADER else None
    async with _scrape_lock:
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT_SECONDS, trust_env=False
        ) as client:
            started_at = time.perf_counter()
            try:
                nginx_response = await client.get(
                    NGINX_STATUS_URL, headers=headers
                )
                nginx_response.raise_for_status()
                _update_nginx_metrics(nginx_response.text)
                NGINX_SCRAPE_SUCCESS.set(1)
            except (httpx.HTTPError, ValueError):
                NGINX_SCRAPE_SUCCESS.set(0)
                NGINX_SCRAPE_ERRORS.inc()
                logger.exception("Failed to collect Nginx stub_status metrics")
            finally:
                NGINX_SCRAPE_DURATION.observe(
                    time.perf_counter() - started_at
                )

            started_at = time.perf_counter()
            try:
                nest_response = await client.get(
                    NEST_METRICS_URL, headers=headers
                )
                nest_response.raise_for_status()
                nest_metrics = nest_response.text
                if not list(text_string_to_metric_families(nest_metrics)):
                    raise ValueError("NestJS metrics response was empty")
                NEST_SCRAPE_SUCCESS.set(1)
            except (httpx.HTTPError, ValueError):
                NEST_SCRAPE_SUCCESS.set(0)
                NEST_SCRAPE_ERRORS.inc()
                logger.exception("Failed to collect NestJS metrics")
            finally:
                NEST_SCRAPE_DURATION.observe(
                    time.perf_counter() - started_at
                )

    sidecar_metrics = generate_latest(REGISTRY).decode("utf-8")
    combined_metrics = f"{nest_metrics.rstrip()}\n{sidecar_metrics}"
    return Response(content=combined_metrics, media_type=CONTENT_TYPE_LATEST)


@app.post("/ask-rag")
async def ask_rag(payload: AskRagRequest) -> dict[str, str]:
    with RAG_QUERY_DURATION.time():
        RAG_QUERIES.inc()
        return {
            "answer": "RAG pipeline placeholder; no model was called.",
        }
