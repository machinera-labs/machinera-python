from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from machinera import _pool


@pytest.fixture(autouse=True)
def fresh_shared_pools() -> Iterator[None]:
    """Give every test its own process-wide pools, as tests patch transport classes."""
    _pool.close_all()
    yield
    _pool.close_all()


@pytest.fixture
def sdk_logger() -> Iterator[logging.Logger]:
    logger = logging.getLogger("machinera")
    handlers, level = logger.handlers[:], logger.level
    yield logger
    logger.handlers[:] = handlers
    logger.setLevel(level)
