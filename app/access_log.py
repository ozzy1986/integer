"""uvicorn's access log without the container's own health checks.

Docker probes GET /health from inside the container every few seconds; logged, those lines bury every real
request in `docker compose logs`. A probe from anywhere else (a load balancer, the deploy script) is logged.
"""

import logging

_HEALTH_PATH = "/health"
_LOCAL_CLIENT = "127.0.0.1:"


class _WithoutLocalHealthChecks(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn logs '%s - "%s %s HTTP/%s" %d' with (client, method, path, http_version, status);
        # a record of any other shape is let through untouched.
        args = record.args
        if isinstance(args, tuple) and len(args) == 5:
            client, _method, path, _version, _status = args
            return not (path == _HEALTH_PATH and str(client).startswith(_LOCAL_CLIENT))
        return True


_FILTER = _WithoutLocalHealthChecks()


def hide_local_health_checks() -> None:
    # The same filter object is added at most once, however many times an app factory runs.
    logging.getLogger("uvicorn.access").addFilter(_FILTER)
