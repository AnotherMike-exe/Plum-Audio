#!/usr/bin/env python3
"""
Flask host for Plum-Audio's config REST APIs.

Runs on port 5002 as a SEPARATE process from the mesh API (aiohttp on 5001). The mesh API must live
in the audio event loop (it calls the async router/aggregator), so WSGI Flask can't share it — hence
two processes. This host mounts the settings, integrations and audio blueprints.

CORS here is far simpler than on :5001, because of a fact worth stating plainly: **nothing in the
frontend ever calls a PEER's :5002.** Settings, integrations and audio are all same-origin through
nginx. So this port only needs to recognise pages served by THIS unit — its own IP, its own
hostname, loopback, and whatever `PLUM_ALLOWED_ORIGINS` adds for a dev server. The mesh API's
peer-derived host set is not needed and would be a needless dependency on the audio process.
"""

import logging
import os
import sys

from flask import Flask, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # scripts/ on path
import cors_policy  # noqa: E402

from audio_api import create_audio_blueprint  # noqa: E402
from integrations_api import create_integrations_blueprint  # noqa: E402
from settings_api import SettingsManager, create_settings_blueprint  # noqa: E402

logger = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(__name__)
    # One SettingsManager shared across blueprints so settings + integrations read/write the same file.
    settings_manager = SettingsManager()
    app.register_blueprint(create_settings_blueprint(settings_manager))
    app.register_blueprint(create_integrations_blueprint(settings_manager))
    app.register_blueprint(create_audio_blueprint(settings_manager))

    @app.after_request
    def _cors(response):
        # No Origin: not a browser request. CORS never applied — add nothing, refuse nothing.
        origin = request.headers.get("Origin")
        allow = cors_policy.origin_header(origin, cors_policy.own_hosts())
        if allow is not None:
            response.headers["Access-Control-Allow-Origin"] = allow
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            response.headers["Access-Control-Max-Age"] = "600"
            response.headers["Vary"] = "Origin"
        elif origin:
            logger.warning(
                "CORS: refused origin %r — set PLUM_ALLOWED_ORIGINS to allow it", origin
            )
        return response

    return app


def main() -> None:
    logging.basicConfig(level=os.environ.get("PLUM_LOG_LEVEL", "INFO"))
    port = int(os.environ.get("PLUM_CONFIG_API_PORT", "5002"))
    logger.info("Plum-Audio config API (Flask) listening on 0.0.0.0:%d", port)
    create_app().run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
