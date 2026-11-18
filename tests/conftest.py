"""Every test runs against the in-memory store. If MONGO_URL is set (as in CI),
every test also runs against a real MongoDB, each in a throwaway database."""

import os
import uuid

import pytest

from issuehub.service import IssueService
from issuehub.store import MemoryStore

MONGO_URL = os.environ.get("MONGO_URL")
BACKENDS = ["memory"] + (["mongo"] if MONGO_URL else [])


@pytest.fixture(params=BACKENDS)
def make_service(request):
    """Factory: each call returns a service backed by fresh, empty storage."""
    if request.param == "memory":
        yield lambda: IssueService(MemoryStore())
        return

    from pymongo import MongoClient

    from issuehub.mongo import MongoStore

    client = MongoClient(MONGO_URL, tz_aware=True, serverSelectionTimeoutMS=3000)
    created: list[str] = []

    def factory():
        name = f"issuehub_test_{uuid.uuid4().hex[:12]}"
        created.append(name)
        store = MongoStore(client[name])
        store.ensure_indexes()
        return IssueService(store)

    try:
        yield factory
    finally:
        for name in created:
            client.drop_database(name)
        client.close()
