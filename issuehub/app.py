"""Runnable Flask server.

    ISSUEHUB_TOKENS="alice-token:alice,bob-token:bob" \
    MONGO_URL=mongodb://localhost:27017 python -m issuehub.app

Without MONGO_URL it uses the in-memory store (data is lost on restart).

Then, for example:

    curl -X POST localhost:8000/projects -H "Authorization: Bearer alice-token" \
         -H "Content-Type: application/json" -d '{"id": "web", "name": "Website"}'
"""

import json
import logging
import os

from flask import Flask, Response, request

from .http import HttpAdapter, StaticTokenAuth
from .service import IssueService
from .store import MemoryStore

log = logging.getLogger(__name__)


def _parse_tokens(raw: str) -> dict[str, str]:
    tokens = {}
    for pair in filter(None, (p.strip() for p in raw.split(","))):
        token, _, user = pair.partition(":")
        if token and user:
            tokens[token] = user
    return tokens


def _store_from_env():
    url = os.environ.get("MONGO_URL")
    if not url:
        log.warning("MONGO_URL not set: using in-memory storage, data is lost on restart")
        return MemoryStore()
    from .mongo import connect

    return connect(url, os.environ.get("MONGO_DB", "issuehub"))


def create_app(adapter: HttpAdapter | None = None) -> Flask:
    if adapter is None:
        tokens = _parse_tokens(os.environ.get("ISSUEHUB_TOKENS", ""))
        adapter = HttpAdapter(IssueService(_store_from_env()), StaticTokenAuth(tokens))

    app = Flask(__name__)

    @app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    @app.route("/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def dispatch(path):
        status, headers, body = adapter.handle(
            request.method,
            "/" + path,
            query=request.args.to_dict(),
            headers=dict(request.headers),
            body=request.get_data(),
        )
        return Response(
            json.dumps(body) if body is not None else "",
            status=status,
            headers=headers,
            mimetype="application/json",
        )

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
