import functools

import pytest

from ai_drone_tune.sim import SimConfig, simulate


@functools.lru_cache(maxsize=None)
def _sim(duration: float = 12.0, rpm: bool = True, seed: int = 3) -> bytes:
    return simulate(SimConfig(duration_s=duration, rpm_filter=rpm, seed=seed))


@pytest.fixture(scope="session")
def sim_log_bytes() -> bytes:
    return _sim()


@pytest.fixture(scope="session")
def sim_log_bytes_norpm() -> bytes:
    return _sim(rpm=False)
