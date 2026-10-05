import time
import uuid

import pytest
from aeoi_common import (
    BaseServiceSettings,
    Environment,
    NotFoundError,
    get_correlation_id,
    new_correlation_id,
    set_correlation_id,
    uuid7,
)
from aeoi_common.ids import uuid7_timestamp_ms


class TestUuid7:
    def test_version_and_variant(self) -> None:
        value = uuid7()
        assert value.version == 7
        assert value.variant == uuid.RFC_4122

    def test_embeds_current_time(self) -> None:
        before = time.time_ns() // 1_000_000
        value = uuid7()
        after = time.time_ns() // 1_000_000
        assert before <= uuid7_timestamp_ms(value) <= after

    def test_sorts_by_time(self) -> None:
        ids = [uuid7(timestamp_ms=1_700_000_000_000 + i) for i in range(50)]
        assert sorted(ids) == ids

    def test_unique(self) -> None:
        assert len({uuid7() for _ in range(10_000)}) == 10_000

    def test_rejects_out_of_range_timestamp(self) -> None:
        with pytest.raises(ValueError, match="48 bits"):
            uuid7(timestamp_ms=2**48)


class TestCorrelation:
    def test_new_sets_and_returns(self) -> None:
        cid = new_correlation_id()
        assert get_correlation_id() == cid
        set_correlation_id(None)
        assert get_correlation_id() is None


class TestErrors:
    def test_problem_detail_carries_status_and_correlation(self) -> None:
        set_correlation_id("corr-1")
        problem = NotFoundError("incident INC-1 not found").to_problem(
            instance="/api/v1/incidents/1"
        )
        assert problem.status == 404
        assert problem.type.endswith("/not-found")
        assert problem.correlation_id == "corr-1"
        set_correlation_id(None)


class TestSettings:
    def test_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AEOI_ENV", "production")
        monkeypatch.setenv("AEOI_SERVICE_NAME", "api")
        settings = BaseServiceSettings()
        assert settings.environment is Environment.PRODUCTION
        assert settings.is_production
        assert settings.service_name == "api"

    def test_database_url_is_secret(self) -> None:
        settings = BaseServiceSettings(database_url="postgresql://u:pw@h/db")  # type: ignore[arg-type]
        assert "pw" not in repr(settings)
