import datetime

import pytest

from main import Clock, DailyReset, FixedClock, NeverReset, QuotaResetPolicy, SystemClock

DAY = datetime.timedelta(days=1)


@pytest.mark.parametrize("cls", [Clock, QuotaResetPolicy])
def test_bases_are_abstract(cls):
    with pytest.raises(TypeError):
        cls()


class TestClocks:
    def test_system_clock_returns_now(self):
        before = datetime.datetime.now()
        assert before <= SystemClock().now() <= datetime.datetime.now()

    def test_fixed_clock(self, noon):
        clock = FixedClock(noon)
        assert clock.now() == noon
        clock.advance(datetime.timedelta(minutes=5))
        assert clock.now() == noon + datetime.timedelta(minutes=5)
        clock.set(noon)
        assert clock.now() == noon


class TestDailyReset:
    def test_no_reset_on_first_tick(self, noon):
        assert not DailyReset().should_reset(noon)

    def test_no_reset_within_same_day(self, noon):
        policy = DailyReset()
        policy.should_reset(noon)
        assert not policy.should_reset(noon + datetime.timedelta(hours=11))

    def test_resets_once_on_new_day(self, noon):
        policy = DailyReset()
        policy.should_reset(noon)
        assert policy.should_reset(noon + DAY)
        assert not policy.should_reset(noon + DAY)

    def test_clock_going_backwards_does_not_reset(self, noon):
        policy = DailyReset()
        policy.should_reset(noon)
        assert not policy.should_reset(noon - DAY)
        assert not policy.should_reset(noon)


def test_never_reset(noon):
    policy = NeverReset()
    assert not policy.should_reset(noon)
    assert not policy.should_reset(noon + DAY)
