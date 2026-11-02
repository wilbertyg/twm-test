import datetime

import pytest

from main import ExecutorRegistry, Scheduler, Task, TaskExecutor, UserManagement

NOON = datetime.datetime(2026, 11, 2, 12, 0, 15)


class RecordingExecutor(TaskExecutor):
    action = "record"

    def __init__(self) -> None:
        self.calls: list[Task] = []

    async def execute(self, task: Task) -> None:
        self.calls.append(task)


class FailingExecutor(TaskExecutor):
    action = "fail"

    async def execute(self, task: Task) -> None:
        raise RuntimeError("boom")


@pytest.fixture
def noon() -> datetime.datetime:
    return NOON


@pytest.fixture
def recorder() -> RecordingExecutor:
    return RecordingExecutor()


@pytest.fixture
def users() -> UserManagement:
    users = UserManagement()
    users.add_user("alice", quota=2)
    users.add_user("bob", quota=1)
    return users


@pytest.fixture
def registry(recorder: RecordingExecutor) -> ExecutorRegistry:
    registry = ExecutorRegistry.with_defaults()
    registry.register(recorder)
    registry.register(FailingExecutor())
    return registry


@pytest.fixture
def scheduler(users: UserManagement, registry: ExecutorRegistry) -> Scheduler:
    return Scheduler(users=users, executors=registry)
