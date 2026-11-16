"""HTTP routes, grouped by concern."""

from flask import Flask


def register_blueprints(app: Flask) -> None:
    from . import api, index, ops, web

    app.register_blueprint(index.bp)
    app.register_blueprint(api.bp)
    app.register_blueprint(ops.bp)
    app.register_blueprint(web.bp)
