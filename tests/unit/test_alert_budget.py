"""Phase 29 gate fix5 (ADR-044 amendment): the alert path's time budget, as a test.

Measured on the Mac (Tempo traces of the 4 slowest alert requests, 2026-10-29): before
incident-service starts the request 1.3-1.5 s can pass on a CPU-starved node; the DB work is
0.3-0.7 s. The catalog lookup must fit in what is left of the api's upstream timeout, with room:
the first version's "2 s" httpx timeout waited up to 4.0 s and the api answered 503 for
incidents that were created."""

from __future__ import annotations

import pytest

from aeoi_api.config import Settings
from aeoi_incident import catalog

pytestmark = pytest.mark.unit

STARVED_START_S = 1.5  # measured: api -> incident-service handler start, worst of 4 traces
DB_WORK_S = 0.7  # measured: incident create + dedupe rows, worst of 4 traces
MARGIN_S = 1.0  # at least this much left over


def test_the_catalog_deadline_fits_inside_the_api_upstream_timeout() -> None:
    api_timeout = Settings.model_fields["upstream_timeout_s"].default
    assert api_timeout >= STARVED_START_S + catalog.DEADLINE_S + DB_WORK_S + MARGIN_S
