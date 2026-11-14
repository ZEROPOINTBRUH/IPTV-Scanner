"""Flask app singleton + factory. Kept tiny so :mod:`main` (and tests) can spin
up the app without worrying about route module import order.

Usage::

    from app_factory import create_app
    app = create_app()

or, when routes have already been registered elsewhere::

    from app_factory import app
"""

import os

from flask import Flask
from flask_cors import CORS

_ROOT = os.path.abspath(os.path.dirname(__file__))
_TEMPLATES = os.path.join(_ROOT, "templates")
_STATIC = os.path.join(_ROOT, "static")

# Module-level singleton so @app.route decorators in routes.py have something
# to attach to at import time (mirroring the original monolithic main.py).
#
# Standard Flask layout: Jinja under templates/, assets under static/
# served at /static/... Explicit @app.route handlers still take priority.
app = Flask(
    __name__,
    template_folder=_TEMPLATES,
    static_folder=_STATIC,
    static_url_path="/static",
)
CORS(app)


def create_app():
    """Return the configured Flask app, ensuring routes are registered."""
    # Side-effect import: registers every @app.route on the singleton above.
    import routes  # noqa: F401
    from features.public_api import register_public_api
    from features.public_base import register_public_base_hooks

    register_public_base_hooks(app)
    register_public_api(app)
    return app
