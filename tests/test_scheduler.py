import asyncio
import datetime
import logging

import pytest

import main
from main import (
    ExecutorRegistry,
    FixedClock,
    InMemoryTaskRepository,
    NeverReset,
    QuotaResetPolicy,
    Scheduler,
    TaskExecutor,
    TaskManagement,
    TaskNotFoundError,
    UnknownActionError,
    UserHasTasksError,
    UserNotFoundError,
    build_scheduler,
)


def test_build_scheduler():
    scheduler = build_scheduler()
    assert {u.name for u in scheduler.users} == {"alice", "bob"}
    assert [t.action for t in scheduler.tasks] == ["sync", "backup", "delete"]


def test_repr(scheduler):
    scheduler.add_task("alice", "12:00", "sync", "/a")
    assert repr(scheduler) == "Scheduler(users=2, tasks=1, actions=('backup', 'delete', 'fail', 'record', 'sync'))"


@pytest.mark.parametrize("attr", ["users", "executors", "tasks"])
def test_collaborators_are_read_only(scheduler, attr):
    with pytest.raises(AttributeError):
        setattr(scheduler, attr, None)


class TestTaskManagement:
    def test_ids_are_unique_and_increment(self, scheduler):
        first = scheduler.add_task("alice", "12:00", "sync", "/a")
        second = scheduler.add_task("alice", "12:00", "sync", "/b")
        assert (first.id, second.id) == (1, 2)

    def test_ids_not_reused_after_removal(self, scheduler):
        first = scheduler.add_task("alice", "12:00", "sync", "/a")
        scheduler.remove_task(first.id)
        assert scheduler.add_task("alice", "12:00", "sync", "/b").id == 2

    def test_get_task(self, scheduler):
        task = scheduler.add_task("alice", "12:00", "sync", "/a")
        assert scheduler.get_task(task.id) is task

    def test_get_unknown_task_raises(self, scheduler):
        with pytest.raises(TaskNotFoundError):
            scheduler.get_task(99)

    def test_remove_task(self, scheduler):
        task = scheduler.add_task("alice", "12:00", "sync", "/a")
        scheduler.remove_task(task.id)
        assert len(scheduler.tasks) == 0

    def test_remove_unknown_task_raises(self, scheduler):
        with pytest.raises(TaskNotFoundError):
            scheduler.remove_task(99)

    def test_for_user(self, scheduler):
        a = scheduler.add_task("alice", "12:00", "sync", "/a")
        scheduler.add_task("bob", "12:00", "sync", "/b")
        assert scheduler.tasks.for_user("alice") == [a]

    def test_uses_injected_repository(self, users, registry):
        repo = InMemoryTaskRepository()
        scheduler = Scheduler(users, registry, tasks=TaskManagement(repo))
        task = scheduler.add_task("alice", "12:00", "sync", "/a")
        assert repo.get(task.id) is task


class TestAddTask:
    def test_parses_time_and_stores_task(self, scheduler):
        task = scheduler.add_task("alice", "08:30", "sync", "/data/x")
        assert task.time == datetime.time(8, 30)
        assert task.last_run is None
        assert list(scheduler.tasks) == [task]

    def test_unknown_user_rejected(self, scheduler):
        with pytest.raises(UserNotFoundError):
            scheduler.add_task("ghost", "12:00", "sync", "/data/x")
        assert len(scheduler.tasks) == 0

    def test_unknown_action_rejected(self, scheduler):
        with pytest.raises(UnknownActionError):
            scheduler.add_task("alice", "12:00", "format", "/data/x")
        assert len(scheduler.tasks) == 0

    @pytest.mark.parametrize("at", ["25:00", "noon", "12-00", ""])
    def test_invalid_time_rejected(self, scheduler, at):
        with pytest.raises(ValueError):
            scheduler.add_task("alice", at, "sync", "/data/x")
        assert len(scheduler.tasks) == 0


class TestRemoveUser:
    def test_removes_user_without_tasks(self, scheduler):
        scheduler.remove_user("bob")
        assert "bob" not in scheduler.users

    def test_refused_while_user_owns_tasks(self, scheduler):
        scheduler.add_task("bob", "12:00", "sync", "/a")
        scheduler.add_task("bob", "13:00", "sync", "/b")
        with pytest.raises(UserHasTasksError, match="2 task"):
            scheduler.remove_user("bob")
        assert "bob" in scheduler.users

    def test_unknown_user_raises(self, scheduler):
        with pytest.raises(UserNotFoundError):
            scheduler.remove_user("ghost")


class TestRunPending:
    def test_runs_due_tasks_only(self, scheduler, recorder, noon):
        due = scheduler.add_task("alice", "12:00", "record", "/a")
        scheduler.add_task("alice", "13:00", "record", "/b")
        assert asyncio.run(scheduler.run_pending(noon)) == [due]
        assert recorder.calls == [due]
        assert due.last_run == noon.date()

    def test_defaults_to_injected_clock(self, users, registry, recorder, noon):
        clock = FixedClock(noon)
        scheduler = Scheduler(users, registry, clock=clock)
        task = scheduler.add_task("alice", "12:00", "record", "/a")
        assert asyncio.run(scheduler.run_pending()) == [task]
        clock.advance(datetime.timedelta(days=1))
        assert asyncio.run(scheduler.run_pending()) == [task]
        assert len(recorder.calls) == 2

    def test_consumes_quota_on_success(self, scheduler, users, noon):
        scheduler.add_task("alice", "12:00", "record", "/a")
        asyncio.run(scheduler.run_pending(noon))
        assert users.get_user("alice").executed == 1

    def test_runs_once_per_day(self, scheduler, recorder, noon):
        scheduler.add_task("alice", "12:00", "record", "/a")
        asyncio.run(scheduler.run_pending(noon))
        assert asyncio.run(scheduler.run_pending(noon + datetime.timedelta(seconds=30))) == []
        assert len(recorder.calls) == 1

    def test_skips_when_quota_reached(self, scheduler, recorder, users, noon, caplog):
        first = scheduler.add_task("bob", "12:00", "record", "/a")
        second = scheduler.add_task("bob", "12:00", "record", "/b")
        with caplog.at_level(logging.WARNING):
            assert asyncio.run(scheduler.run_pending(noon)) == [first]
        assert recorder.calls == [first]
        assert users.get_user("bob").executed == 1
        assert second.last_run == noon.date()
        assert "bob has reached their quota (1)" in caplog.text

    def test_quota_is_per_user(self, scheduler, noon):
        scheduler.add_task("bob", "12:00", "record", "/a")
        scheduler.add_task("bob", "12:00", "record", "/b")
        alice_task = scheduler.add_task("alice", "12:00", "record", "/c")
        assert alice_task in asyncio.run(scheduler.run_pending(noon))

    def test_failure_is_logged_and_does_not_consume_quota(self, scheduler, users, noon, caplog):
        failing = scheduler.add_task("alice", "12:00", "fail", "/a")
        ok = scheduler.add_task("alice", "12:00", "record", "/b")
        with caplog.at_level(logging.ERROR):
            assert asyncio.run(scheduler.run_pending(noon)) == [ok]
        assert failing.last_run == noon.date()
        assert users.get_user("alice").executed == 1
        assert "Task fail on /a for alice failed" in caplog.text
        assert "RuntimeError: boom" in caplog.text

    def test_removed_user_is_logged_not_raised(self, scheduler, users, noon, caplog):
        scheduler.add_task("bob", "12:00", "record", "/a")
        users.remove_user("bob")  # bypasses Scheduler.remove_user on purpose
        with caplog.at_level(logging.ERROR):
            assert asyncio.run(scheduler.run_pending(noon)) == []
        assert "UserNotFoundError" in caplog.text

    def test_delete_outside_tmp_is_refused(self, scheduler, users, noon, caplog):
        scheduler.add_task("alice", "12:00", "delete", "/etc/passwd")
        with caplog.at_level(logging.ERROR):
            assert asyncio.run(scheduler.run_pending(noon)) == []
        assert "PermissionError" in caplog.text
        assert users.get_user("alice").executed == 0


class TestQuotaReset:
    def test_daily_reset_is_default(self, scheduler, recorder, users, noon):
        scheduler.add_task("bob", "12:00", "record", "/a")
        asyncio.run(scheduler.run_pending(noon))
        assert users.get_user("bob").remaining == 0

        assert len(asyncio.run(scheduler.run_pending(noon + datetime.timedelta(days=1)))) == 1
        assert users.get_user("bob").executed == 1
        assert len(recorder.calls) == 2

    def test_no_reset_on_first_run(self, scheduler, users, noon):
        users.get_user("bob").consume()
        scheduler.add_task("bob", "12:00", "record", "/a")
        assert asyncio.run(scheduler.run_pending(noon)) == []

    def test_never_reset_policy(self, users, registry, noon):
        scheduler = Scheduler(users, registry, reset_policy=NeverReset())
        scheduler.add_task("bob", "12:00", "record", "/a")
        asyncio.run(scheduler.run_pending(noon))
        assert asyncio.run(scheduler.run_pending(noon + datetime.timedelta(days=1))) == []
        assert users.get_user("bob").executed == 1

    def test_custom_policy(self, users, registry, noon):
        class EveryTick(QuotaResetPolicy):
            def should_reset(self, now):
                return True

        scheduler = Scheduler(users, registry, reset_policy=EveryTick())
        users.get_user("bob").consume()
        asyncio.run(scheduler.run_pending(noon))
        assert users.get_user("bob").executed == 0


class TestAsync:
    def test_due_tasks_run_concurrently(self, users, noon):
        class SlowExecutor(TaskExecutor):
            action = "slow"

            def __init__(self) -> None:
                self.running = 0
                self.peak = 0

            async def execute(self, task):
                self.running += 1
                self.peak = max(self.peak, self.running)
                await asyncio.sleep(0.01)
                self.running -= 1

        slow = SlowExecutor()
        scheduler = Scheduler(users, ExecutorRegistry([slow]))
        tasks = [scheduler.add_task("alice", "12:00", "slow", f"/{i}") for i in range(2)]
        assert asyncio.run(scheduler.run_pending(noon)) == tasks
        assert slow.peak == 2

    def test_concurrent_tasks_cannot_overrun_quota(self, users, noon):
        class SlowExecutor(TaskExecutor):
            action = "slow"

            async def execute(self, task):
                await asyncio.sleep(0.01)

        scheduler = Scheduler(users, ExecutorRegistry([SlowExecutor()]))
        first = scheduler.add_task("bob", "12:00", "slow", "/a")  # bob's quota is 1
        scheduler.add_task("bob", "12:00", "slow", "/b")
        assert asyncio.run(scheduler.run_pending(noon)) == [first]
        assert users.get_user("bob").executed == 1


class TestRunForever:
    def test_polls_until_cancelled(self, scheduler, monkeypatch, caplog):
        calls = []
        sleeps = []

        async def fake_run_pending():
            calls.append(1)

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                raise asyncio.CancelledError

        monkeypatch.setattr(scheduler, "run_pending", fake_run_pending)
        monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

        with caplog.at_level(logging.INFO), pytest.raises(asyncio.CancelledError):
            asyncio.run(scheduler.run_forever(interval=5))

        assert len(calls) == 3
        assert sleeps == [5, 5, 5]
        assert "Scheduler stopped" in caplog.text
