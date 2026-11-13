"""A second session must not commit or roll back a worker's unfinished reservation."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import app.storage_quota as quota
from app.db import create_sqlite_engine, get_session_factory
from app.main import app
from custombuild_worker import furniture_review, tasks
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.furniture_review_storage import install_review_storage
from tests.integration.test_furniture_workspace import HEADERS, TENANT, request_review


def test_overlapping_session_cannot_erase_an_uncommitted_export_reservation(monkeypatch):
    storage = install_review_storage(monkeypatch)
    monkeypatch.setattr(
        furniture_review, "build_furniture_review", lambda document: b"review-binary"
    )
    original_insert = quota._insert_stored_object
    ready, finish = Event(), Event()

    def overlapping_reader():
        with get_session_factory()() as reader:
            reader.execute(text("SELECT 1"))
            ready.set()
            if not finish.wait(10):
                raise TimeoutError("export did not reach the reservation")
            reader.rollback()

    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as threads:
        _, _, path, body, job = request_review(client)
        pending = threads.submit(overlapping_reader)
        try:
            assert ready.wait(5)

            def overlap(*args, **kwargs):
                inserted = original_insert(*args, **kwargs)
                finish.set()
                pending.result(timeout=5)
                return inserted

            monkeypatch.setattr(quota, "_insert_stored_object", overlap)
            assert tasks.generate_furniture_review.run(job, TENANT)["state"] == "succeeded"
        finally:
            finish.set()
        assert len(storage.put_calls) == 1
        assert client.post(path, headers=HEADERS, json=body).json()["job_id"] == job
        assert client.get(f"{path}/{job}/download", headers=HEADERS).content == b"review-binary"


def test_anonymous_sqlite_engines_remain_private_and_file_databases_persist(tmp_path):
    url = "sqlite+pysqlite:///:memory:"
    first, second = create_sqlite_engine(url), create_sqlite_engine(url)
    try:
        with first.begin() as writer:
            writer.execute(text("CREATE TABLE reservation_check(value integer)"))
        with first.connect() as a, first.connect() as b, second.connect() as other:
            assert a.connection.driver_connection is not b.connection.driver_connection
            query = text("SELECT count(*) FROM sqlite_master WHERE name='reservation_check'")
            assert b.scalar(query) == 1 and other.scalar(query) == 0
            assert a.execute(text("PRAGMA database_list")).one()[2] == ""
    finally:
        first.dispose()
        second.dispose()
    file_url = f"sqlite+pysqlite:///{tmp_path / 'orders.db'}"
    engine = create_sqlite_engine(file_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE reservation_check(value integer)"))
    finally:
        engine.dispose()
    reopened = create_sqlite_engine(file_url)
    try:
        with reopened.connect() as connection:
            assert connection.scalar(query) == 1
    finally:
        reopened.dispose()
