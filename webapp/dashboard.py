"""8091 presentation entrypoint; all routes/data belong to the original Flask app.

Only the root URL is mapped to the new template. No separate business backend,
data service, scanner, or changes to the 8090 WSGI application's root mapping.
Run from the project root: python -m webapp.dashboard
"""
from werkzeug.serving import run_simple

from webapp.app import app


def application(environ, start_response):
    if environ.get('PATH_INFO') == '/':
        environ = dict(environ, PATH_INFO='/dashboard')
    return app(environ, start_response)


if __name__ == '__main__':
    run_simple('127.0.0.1', 8091, application, threaded=True, use_reloader=False)
