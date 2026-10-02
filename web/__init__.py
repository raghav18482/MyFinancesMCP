"""Web dashboard package.

Layout::

    web/app.py           create_app(): FastAPI instance, middleware, mounts
    web/dependencies.py  request-scoped helpers every router shares
    web/templating.py    Jinja2 loader
    web/view_models.py   row shapes handed to templates
    web/routers/         one module per domain, listed in routers/__init__.py

Business logic lives under ``services/``; routers translate HTTP to and from
those calls and should stay thin.

Importing this package has no side effects. Call :func:`web.app.create_app`
to build the application.
"""
