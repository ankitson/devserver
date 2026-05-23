"""Dagster resources wrapping pipeline-shared.

The resource caches one `GarminRun` per Dagster run_id at module scope so all
ops within a single run share a single GarminClient login. Combined with the
in-process executor on Definitions, that means 1 login per partition, not 1
login per op (the previous behaviour cost us ~20 s × 7 ops per cache-miss
partition just on auth).
"""

import threading

from dagster import ConfigurableResource

from pipeline_shared import GarminRun, load_settings
from pipeline_shared.config import Settings


# run_id -> GarminRun (with its lazy-loaded GarminClient). Module-level so it
# survives across the multiple op invocations in a single Dagster run. Each
# new run_id is a fresh GarminRun → fresh client at first API call → token
# load/save handled inside pipeline_shared.garmin.GarminRun.get_client().
_RUN_CACHE: dict[str, GarminRun] = {}
_RUN_CACHE_LOCK = threading.Lock()


class GarminPipelineResource(ConfigurableResource):
    """Wraps Settings + a per-run GarminRun (cached at module scope)."""

    tool: str = "dagster"

    def settings(self) -> Settings:
        return load_settings()

    def make_run(self, run_id: str) -> GarminRun:
        with _RUN_CACHE_LOCK:
            existing = _RUN_CACHE.get(run_id)
            if existing is not None:
                return existing
            s = self.settings()
            run = GarminRun(
                settings=s, target_url=s.database_url,
                source_url=s.garmin_source_database_url,
                tool=self.tool, run_id=run_id,
            )
            _RUN_CACHE[run_id] = run
            return run
