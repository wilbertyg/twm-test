import datetime

import pytest

from main import QuotaReachedError, SchedulerError, Task, User


class TestUser:
    def test_defaults(self):
        user = User(name="alice", quota=3)
        assert user.executed == 0
        assert user.remaining == 3
        assert user.has_quota()

    def test_repr_shows_executed(self):
        user = User(name="alice", quota=3)
        user.consume()
        assert repr(user) == "User(name='alice', quota=3, executed=1)"

    @pytest.mark.parametrize("kwargs", [{"name": "alice", "quota": -1}, {"name": "", "quota": 1}])
    def test_invalid_values_rejected(self, kwargs):
        with pytest.raises(ValueError):
            User(**kwargs)

    def test_executed_is_encapsulated(self):
        with pytest.raises(TypeError):
            User(name="alice", quota=3, executed=2)
        with pytest.raises(AttributeError):
            User(name="alice", quota=3).executed = 2

    def test_zero_quota_has_no_quota(self):
        assert not User(name="alice", quota=0).has_quota()

    def test_consume_until_quota_reached(self):
        user = User(name="alice", quota=2)
        user.consume()
        user.consume()
        assert user.executed == 2
        assert user.remaining == 0
        assert not user.has_quota()

    def test_consume_beyond_quota_raises(self):
        user = User(name="alice", quota=1)
        user.consume()
        with pytest.raises(QuotaReachedError) as exc:
            user.consume()
        assert isinstance(exc.value, SchedulerError)
        assert user.executed == 1

    def test_remaining_never_negative_when_quota_lowered(self):
        user = User(name="alice", quota=2)
        user.consume()
        user.consume()
        user.quota = 1
        assert user.remaining == 0

    def test_reset(self):
        user = User(name="alice", quota=2)
        user.consume()
        user.reset()
        assert user.executed == 0
        assert user.has_quota()


class TestTask:
    @pytest.fixture
    def task(self) -> Task:
        return Task(user="alice", time=datetime.time(12, 0), action="sync", target="/data/x", id=7)

    def test_repr(self, task):
        assert repr(task) == "Task(id=7, user='alice', time=12:00, action='sync', target='/data/x', last_run=None)"

    def test_due_at_matching_minute(self, task, noon):
        assert task.is_due(noon)

    def test_seconds_are_ignored(self, task):
        assert task.is_due(datetime.datetime(2026, 11, 2, 12, 0, 59))

    @pytest.mark.parametrize("hour, minute", [(12, 1), (11, 59), (0, 0)])
    def test_not_due_at_other_minutes(self, task, hour, minute):
        assert not task.is_due(datetime.datetime(2026, 11, 2, hour, minute))

    def test_not_due_once_handled_today(self, task, noon):
        task.mark_handled(noon.date())
        assert task.last_run == noon.date()
        assert not task.is_due(noon)

    def test_due_again_the_next_day(self, task, noon):
        task.mark_handled(noon.date() - datetime.timedelta(days=1))
        assert task.is_due(noon)

    def test_last_run_is_read_only(self, task, noon):
        with pytest.raises(AttributeError):
            task.last_run = noon.date()


class TestParseTime:
    @pytest.mark.parametrize("at, expected", [("00:00", (0, 0)), ("08:30", (8, 30)), ("23:59", (23, 59))])
    def test_valid(self, at, expected):
        assert Task.parse_time(at) == datetime.time(*expected)

    @pytest.mark.parametrize("at", ["24:00", "12:60", "12-00", "12:00+07:00", "noon", ""])
    def test_invalid(self, at):
        with pytest.raises(ValueError):
            Task.parse_time(at)
