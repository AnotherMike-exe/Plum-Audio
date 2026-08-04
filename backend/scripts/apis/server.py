#!/usr/bin/env python3
"""
Flask host for Plum-Audio's config REST APIs.

Runs on port 5002 as a SEPARATE process from the mesh API (aiohttp on 5001). The mesh API must live
in the audio event loop (it calls the async router/aggregator), so WSGI Flask can't share it — hence
two processes. This host currently mounts the settings blueprint; the integrations/audio blueprints
slot in here alongside their Phase-3 source services.
"""

import logging
import os

from flask import Flask
from flask_cors import CORS

from audio_api import create_audio_blueprint
from integrations_api import create_integrations_blueprint
from settings_api import SettingsManager, create_settings_blueprint

logger = logging.getLogger(__name__)


def create_app() -> Flask:
    app = Flask(__name__)
    CORS(app)  # blanket CORS — the GUI is served from a different origin (Vite dev / proxy)
    # One SettingsManager shared across blueprints so settings + integrations read/write the same file.
    settings_manager = SettingsManager()
    app.register_blueprint(create_settings_blueprint(settings_manager))
    app.register_blueprint(create_integrations_blueprint(settings_manager))
    app.register_blueprint(create_audio_blueprint(settings_manager))
    return app


def main() -> None:
    logging.basicConfig(level=os.environ.get("PLUM_LOG_LEVEL", "INFO"))
    port = int(os.environ.get("PLUM_CONFIG_API_PORT", "5002"))
    logger.info("Plum-Audio config API (Flask) listening on 0.0.0.0:%d", port)
    create_app().run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
