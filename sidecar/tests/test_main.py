import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from app import main


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class FakeAsyncClient:
    def __init__(self, **_: object) -> None:
        self.responses = iter(
            (
                FakeResponse(
                    "Active connections: 3\n"
                    "server accepts handled requests\n"
                    "12 12 35\n"
                    "Reading: 1 Writing: 2 Waiting: 0\n"
                ),
                FakeResponse("nodejs_heap_size_used_bytes 1234\n"),
            )
        )

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def get(self, _: str, **__: object) -> FakeResponse:
        return next(self.responses)


class SidecarTests(unittest.TestCase):
    def test_metrics_combines_nest_nginx_and_python_metrics(self) -> None:
        with patch("app.main.httpx.AsyncClient", FakeAsyncClient):
            with TestClient(main.app) as client:
                response = client.get("/metrics")

        self.assertEqual(response.status_code, 200)
        self.assertIn("nodejs_heap_size_used_bytes 1234", response.text)
        self.assertIn("nginx_connections_active 3.0", response.text)
        self.assertIn("python_process_cpu_seconds_total", response.text)
        self.assertIn("nginx_scrape_success 1.0", response.text)
        family_names = [
            family.name
            for family in text_string_to_metric_families(response.text)
        ]
        self.assertEqual(len(family_names), len(set(family_names)))

    def test_ask_rag_counts_valid_queries(self) -> None:
        before = main.RAG_QUERIES._value.get()
        with TestClient(main.app) as client:
            response = client.post(
                "/ask-rag", json={"question": "Where is my order?"}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(main.RAG_QUERIES._value.get(), before + 1)
        self.assertIn("RAG pipeline placeholder", response.json()["answer"])

    def test_ask_rag_rejects_empty_questions(self) -> None:
        before = main.RAG_QUERIES._value.get()
        with TestClient(main.app) as client:
            response = client.post("/ask-rag", json={"question": ""})

        self.assertEqual(response.status_code, 422)
        self.assertEqual(main.RAG_QUERIES._value.get(), before)


if __name__ == "__main__":
    unittest.main()
