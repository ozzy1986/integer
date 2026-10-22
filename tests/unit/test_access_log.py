import logging

import pytest

from app.access_log import hide_local_health_checks

FORMAT = '%s - "%s %s HTTP/%s" %d'  # how uvicorn writes its access log


def test_only_the_containers_own_health_checks_are_left_out(caplog: pytest.LogCaptureFixture) -> None:
    hide_local_health_checks()
    hide_local_health_checks()  # every worker's app factory calls it; the filter must not pile up
    access = logging.getLogger("uvicorn.access")
    requests = [
        ("127.0.0.1:51872", "GET", "/health"),  # docker's HEALTHCHECK inside the container: hidden
        ("172.18.0.1:0", "GET", "/health"),  # someone outside asking: logged
        ("127.0.0.1:51872", "GET", "/health/invariants"),
        ("127.0.0.1:51872", "POST", "/generate"),
    ]

    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        for client, method, path in requests:
            access.info(FORMAT, client, method, path, "1.1", 200)

    assert [record.getMessage() for record in caplog.records] == [
        '172.18.0.1:0 - "GET /health HTTP/1.1" 200',
        '127.0.0.1:51872 - "GET /health/invariants HTTP/1.1" 200',
        '127.0.0.1:51872 - "POST /generate HTTP/1.1" 200',
    ]
    assert len(access.filters) == 1
