import datetime as dt

import pytest

from garmin_autocoach.config import Config
from garmin_autocoach.demo import demo_snapshot

AS_OF = dt.date(2026, 11, 4)  # Wednesday; next Monday 2026-11-09 is ISO week 46 (even, not deload)


@pytest.fixture
def config():
    return Config()


@pytest.fixture
def snap():
    return demo_snapshot(as_of=AS_OF, scenario="steady")
