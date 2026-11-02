"""Simple task scheduling system with per-user quotas.

OOP design
----------
- Abstraction: `Clock`, `UserRepository`, `TaskRepository`, `TaskExecutor` and
  `QuotaResetPolicy` are abstract base classes. `Scheduler` depends only on these
  abstractions, never on a concrete implementation (dependency inversion).
- Encapsulation: mutable state (`User.executed`, `Task.last_run`, repository storage,
  the executor table) is private and changes only through methods that keep invariants.
- Inheritance & polymorphism: executors, repositories, clocks and reset policies are
  interchangeable subclasses of their base class.
- Single responsibility: storage (repositories), business rules (`UserManagement`,
  `TaskManagement`), execution (executors), quota policy and orchestration (`Scheduler`)
  live in separate classes.

Extending with a new action:

    class NotifyExecutor(TaskExecutor):
        action = "notify"

        async def execute(self, task: Task) -> None:
            await send_email(task.user, task.target)

    registry.register(NotifyExecutor())

Executors are async: due tasks in one tick run concurrently.

Configuration (users, tasks, log level, ...) is read from a JSON file, see
`load_config()` and `config.json`.
"""

import asyncio
import contextlib
import copy
import datetime
import json
import logging
import os
from pathlib import Path
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import ClassVar

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class SchedulerError(Exception):
    """Base error for the scheduling system."""


class UserNotFoundError(SchedulerError):
    def __init__(self, name: str) -> None:
        super().__init__(f"User not found: {name}")


class UserAlreadyExistsError(SchedulerError, ValueError):
    def __init__(self, name: str) -> None:
        super().__init__(f"User already exists: {name}")


class UserHasTasksError(SchedulerError):
    def __init__(self, name: str, count: int) -> None:
        super().__init__(f"User {name} still owns {count} task(s)")


class TaskNotFoundError(SchedulerError):
    def __init__(self, task_id: int) -> None:
        super().__init__(f"Task not found: {task_id}")


class QuotaReachedError(SchedulerError):
    def __init__(self, name: str) -> None:
        super().__init__(f"User {name} has reached their quota")


class UnknownActionError(SchedulerError):
    def __init__(self, action: str) -> None:
        super().__init__(f"No executor registered for action: {action}")


# --------------------------------------------------------------------------- #
# Clock
# --------------------------------------------------------------------------- #

class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime.datetime:
        """Current local time."""


class SystemClock(Clock):
    def now(self) -> datetime.datetime:
        return datetime.datetime.now()


class FixedClock(Clock):
    """Manually controlled clock for tests and simulations."""

    def __init__(self, now: datetime.datetime) -> None:
        self._now = now

    def now(self) -> datetime.datetime:
        return self._now

    def set(self, now: datetime.datetime) -> None:
        self._now = now

    def advance(self, delta: datetime.timedelta) -> None:
        self._now += delta


# --------------------------------------------------------------------------- #
# Domain models
# --------------------------------------------------------------------------- #

@dataclass
class User:
    name: str
    quota: int
    _executed: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("name must not be empty")
        if self.quota < 0:
            raise ValueError("quota must be >= 0")

    def __repr__(self) -> str:
        return f"User(name={self.name!r}, quota={self.quota}, executed={self._executed})"

    @property
    def executed(self) -> int:
        return self._executed

    @property
    def remaining(self) -> int:
        return max(self.quota - self._executed, 0)

    def has_quota(self) -> bool:
        return self._executed < self.quota

    def consume(self) -> None:
        if not self.has_quota():
            raise QuotaReachedError(self.name)
        self._executed += 1

    def refund(self) -> None:
        """Give back one execution (used when a reserved run fails)."""
        self._executed = max(self._executed - 1, 0)

    def reset(self) -> None:
        self._executed = 0


@dataclass
class Task:
    user: str
    time: datetime.time
    action: str
    target: str
    id: int = 0  # assigned by TaskManagement
    _last_run: datetime.date | None = field(default=None, init=False)

    def __repr__(self) -> str:
        return (
            f"Task(id={self.id}, user={self.user!r}, time={self.time:%H:%M}, "
            f"action={self.action!r}, target={self.target!r}, last_run={self._last_run})"
        )

    @staticmethod
    def parse_time(at: str) -> datetime.time:
        """Strict "HH:MM" parser; raises ValueError (also for offsets like "12-00")."""
        return datetime.datetime.strptime(at, "%H:%M").time()

    @property
    def last_run(self) -> datetime.date | None:
        return self._last_run

    def is_due(self, now: datetime.datetime) -> bool:
        """Due when the minute matches and it hasn't been handled today."""
        same_minute = (self.time.hour, self.time.minute) == (now.hour, now.minute)
        return same_minute and self._last_run != now.date()

    def mark_handled(self, day: datetime.date) -> None:
        self._last_run = day


# --------------------------------------------------------------------------- #
# Repositories (storage abstraction)
# --------------------------------------------------------------------------- #

class UserRepository(ABC):
    @abstractmethod
    def get(self, name: str) -> User | None: ...

    @abstractmethod
    def save(self, user: User) -> None:
        """Insert or update."""

    @abstractmethod
    def delete(self, name: str) -> None: ...

    @abstractmethod
    def all(self) -> list[User]: ...


class InMemoryUserRepository(UserRepository):
    def __init__(self) -> None:
        self._users: dict[str, User] = {}

    def get(self, name: str) -> User | None:
        return self._users.get(name)

    def save(self, user: User) -> None:
        self._users[user.name] = user

    def delete(self, name: str) -> None:
        self._users.pop(name, None)

    def all(self) -> list[User]:
        return list(self._users.values())


class TaskRepository(ABC):
    @abstractmethod
    def next_id(self) -> int:
        """Reserve a new, never reused id."""

    @abstractmethod
    def get(self, task_id: int) -> Task | None: ...

    @abstractmethod
    def save(self, task: Task) -> None:
        """Insert or update."""

    @abstractmethod
    def delete(self, task_id: int) -> None: ...

    @abstractmethod
    def all(self) -> list[Task]: ...


class InMemoryTaskRepository(TaskRepository):
    def __init__(self) -> None:
        self._tasks: dict[int, Task] = {}
        self._last_id = 0

    def next_id(self) -> int:
        self._last_id += 1
        return self._last_id

    def get(self, task_id: int) -> Task | None:
        return self._tasks.get(task_id)

    def save(self, task: Task) -> None:
        self._tasks[task.id] = task

    def delete(self, task_id: int) -> None:
        self._tasks.pop(task_id, None)

    def all(self) -> list[Task]:
        return list(self._tasks.values())


# --------------------------------------------------------------------------- #
# Management services (business rules on top of storage)
# --------------------------------------------------------------------------- #

class UserManagement:
    def __init__(self, repository: UserRepository | None = None) -> None:
        self._repository = repository if repository is not None else InMemoryUserRepository()

    def __iter__(self) -> Iterator[User]:
        return iter(self._repository.all())

    def __len__(self) -> int:
        return len(self._repository.all())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self._repository.get(name) is not None

    def add_user(self, name: str, quota: int) -> User:
        if name in self:
            raise UserAlreadyExistsError(name)
        user = User(name=name, quota=quota)
        self._repository.save(user)
        log.info("User %s added (quota %d)", name, quota)
        return user

    def get_user(self, name: str) -> User:
        user = self._repository.get(name)
        if user is None:
            raise UserNotFoundError(name)
        return user

    def remove_user(self, name: str) -> None:
        self.get_user(name)
        self._repository.delete(name)
        log.info("User %s removed", name)

    def record_execution(self, user: User) -> None:
        user.consume()
        self._repository.save(user)

    def refund_execution(self, user: User) -> None:
        user.refund()
        self._repository.save(user)

    def reset_quotas(self) -> None:
        for user in self._repository.all():
            user.reset()
            self._repository.save(user)
        log.info("Quotas reset")


class TaskManagement:
    def __init__(self, repository: TaskRepository | None = None) -> None:
        self._repository = repository if repository is not None else InMemoryTaskRepository()

    def __iter__(self) -> Iterator[Task]:
        return iter(self._repository.all())

    def __len__(self) -> int:
        return len(self._repository.all())

    def create(self, user: str, at: datetime.time, action: str, target: str) -> Task:
        task = Task(user=user, time=at, action=action, target=target, id=self._repository.next_id())
        self._repository.save(task)
        log.info("Task %d added: %s %s for %s at %s", task.id, action, target, user, f"{at:%H:%M}")
        return task

    def get(self, task_id: int) -> Task:
        task = self._repository.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        return task

    def remove(self, task_id: int) -> None:
        self.get(task_id)
        self._repository.delete(task_id)
        log.info("Task %d removed", task_id)

    def for_user(self, name: str) -> list[Task]:
        return [task for task in self._repository.all() if task.user == name]

    def mark_handled(self, task: Task, day: datetime.date) -> None:
        task.mark_handled(day)
        self._repository.save(task)


# --------------------------------------------------------------------------- #
# Executors (extendable)
# --------------------------------------------------------------------------- #

class TaskExecutor(ABC):
    """Base class for actions. Subclass, set `action`, implement async `execute`."""

    action: ClassVar[str]

    @abstractmethod
    async def execute(self, task: Task) -> None:
        """Run the task. Raise an exception to signal failure."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}(action={self.action!r})"


class SyncExecutor(TaskExecutor):
    action = "sync"

    async def execute(self, task: Task) -> None:
        log.info("Syncing %s for %s", task.target, task.user)


class BackupExecutor(TaskExecutor):
    action = "backup"

    async def execute(self, task: Task) -> None:
        log.info("Backing up %s for %s", task.target, task.user)


class DeleteExecutor(TaskExecutor):
    action = "delete"

    def __init__(self, allowed_prefixes: Iterable[str] = ("/tmp/",)) -> None:
        self._allowed_prefixes = tuple(allowed_prefixes)

    @property
    def allowed_prefixes(self) -> tuple[str, ...]:
        return self._allowed_prefixes

    async def execute(self, task: Task) -> None:
        if not task.target.startswith(self._allowed_prefixes):
            raise PermissionError(f"Delete outside {self._allowed_prefixes} refused: {task.target}")
        log.info("Deleting %s for %s", task.target, task.user)


class ExecutorRegistry:
    def __init__(self, executors: Iterable[TaskExecutor] = ()) -> None:
        self._executors: dict[str, TaskExecutor] = {}
        for executor in executors:
            self.register(executor)

    @classmethod
    def with_defaults(cls) -> "ExecutorRegistry":
        return cls([SyncExecutor(), BackupExecutor(), DeleteExecutor()])

    def __contains__(self, action: object) -> bool:
        return action in self._executors

    @property
    def actions(self) -> tuple[str, ...]:
        return tuple(sorted(self._executors))

    def register(self, executor: TaskExecutor) -> None:
        """Register (or replace) the executor for `executor.action`."""
        action = getattr(executor, "action", None)
        if not isinstance(action, str) or not action:
            raise ValueError(f"{type(executor).__name__} must define a non-empty `action`")
        self._executors[action] = executor

    def get(self, action: str) -> TaskExecutor:
        try:
            return self._executors[action]
        except KeyError:
            raise UnknownActionError(action) from None


# --------------------------------------------------------------------------- #
# Quota reset policies (strategy)
# --------------------------------------------------------------------------- #

class QuotaResetPolicy(ABC):
    @abstractmethod
    def should_reset(self, now: datetime.datetime) -> bool:
        """Called on every scheduler tick; return True when quotas must reset now."""


class NeverReset(QuotaResetPolicy):
    def should_reset(self, now: datetime.datetime) -> bool:
        return False


class DailyReset(QuotaResetPolicy):
    """Reset on the first tick of each new day (not on the very first tick)."""

    def __init__(self) -> None:
        self._last_day: datetime.date | None = None

    def should_reset(self, now: datetime.datetime) -> bool:
        today = now.date()
        if self._last_day is not None and today <= self._last_day:
            return False
        is_new_day = self._last_day is not None
        self._last_day = today
        return is_new_day


# --------------------------------------------------------------------------- #
# Scheduler (orchestration)
# --------------------------------------------------------------------------- #

class Scheduler:
    def __init__(
        self,
        users: UserManagement,
        executors: ExecutorRegistry,
        *,
        tasks: TaskManagement | None = None,
        reset_policy: QuotaResetPolicy | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._users = users
        self._executors = executors
        self._tasks = tasks if tasks is not None else TaskManagement()
        self._reset_policy = reset_policy if reset_policy is not None else DailyReset()
        self._clock = clock if clock is not None else SystemClock()

    def __repr__(self) -> str:
        return f"Scheduler(users={len(self._users)}, tasks={len(self._tasks)}, actions={self._executors.actions})"

    @property
    def users(self) -> UserManagement:
        return self._users

    @property
    def executors(self) -> ExecutorRegistry:
        return self._executors

    @property
    def tasks(self) -> TaskManagement:
        return self._tasks

    # --- tasks ------------------------------------------------------------ #

    def add_task(self, user: str, at: str, action: str, target: str) -> Task:
        """Add a daily task. `at` is "HH:MM". Validates user, action and time up front."""
        self._users.get_user(user)
        self._executors.get(action)
        return self._tasks.create(user, Task.parse_time(at), action, target)

    def get_task(self, task_id: int) -> Task:
        return self._tasks.get(task_id)

    def remove_task(self, task_id: int) -> None:
        self._tasks.remove(task_id)

    # --- users ------------------------------------------------------------ #

    def remove_user(self, name: str) -> None:
        """Remove a user; refused while they still own tasks."""
        self._users.get_user(name)
        owned = len(self._tasks.for_user(name))
        if owned:
            raise UserHasTasksError(name, owned)
        self._users.remove_user(name)

    # --- running ---------------------------------------------------------- #

    async def run_pending(self, now: datetime.datetime | None = None) -> list[Task]:
        """Run every due task once, concurrently. Returns the tasks that executed successfully."""
        if now is None:
            now = self._clock.now()
        if self._reset_policy.should_reset(now):
            self._users.reset_quotas()
        due = [task for task in self._tasks if task.is_due(now)]
        if due:
            log.debug("%d task(s) due at %s", len(due), f"{now:%H:%M}")
        results = await asyncio.gather(*(self._run_task(task, now) for task in due))
        return [task for task, ok in zip(due, results) if ok]

    async def run_forever(self, interval: float = 30.0) -> None:
        """Poll more than once per minute; `Task.last_run` prevents double runs."""
        log.info("Scheduler started with %d task(s), polling every %ss", len(self._tasks), interval)
        try:
            while True:
                await self.run_pending()
                await asyncio.sleep(interval)
        finally:
            log.info("Scheduler stopped")

    async def _run_task(self, task: Task, now: datetime.datetime) -> bool:
        self._tasks.mark_handled(task, now.date())  # handled for today, whatever the outcome
        try:
            user = self._users.get_user(task.user)
            if not user.has_quota():
                log.warning("%s has reached their quota (%d)", user.name, user.quota)
                return False
            executor = self._executors.get(task.action)
            # Reserve the quota before awaiting so concurrent tasks can't overrun it.
            self._users.record_execution(user)
            try:
                await executor.execute(task)
            except BaseException:
                self._users.refund_execution(user)  # only successful runs count against the quota
                raise
        except Exception:
            log.exception("Task %s on %s for %s failed", task.action, task.target, task.user)
            return False
        log.info("Task %d (%s on %s) for %s done", task.id, task.action, task.target, task.user)
        return True


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

CONFIG_ENV = "SCHEDULER_CONFIG"
DEFAULT_CONFIG_FILE = "config.json"
DEFAULT_CONFIG: dict = {
    "log_level": "INFO",
    "poll_interval": 30.0,
    "delete_allowed_prefixes": ["/tmp/"],
    "users": [
        {"name": "alice", "quota": 3},
        {"name": "bob", "quota": 5},
    ],
    "tasks": [
        {"user": "alice", "at": "12:00", "action": "sync", "target": "/data/x"},
        {"user": "bob", "at": "12:00", "action": "backup", "target": "/srv/y"},
        {"user": "alice", "at": "12:00", "action": "delete", "target": "/tmp/z"},
    ],
}


def load_config(path: str | os.PathLike | None = None) -> dict:
    """Return `DEFAULT_CONFIG` overridden by a JSON file.

    The file is `path`, else `$SCHEDULER_CONFIG`, else `./config.json`. Only the
    implicit `./config.json` may be missing (defaults are used then).
    """
    explicit = path or os.environ.get(CONFIG_ENV)
    file = Path(explicit or DEFAULT_CONFIG_FILE)
    config = copy.deepcopy(DEFAULT_CONFIG)
    if explicit or file.exists():
        data = json.loads(file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{file}: top level must be a JSON object")
        unknown = set(data) - set(DEFAULT_CONFIG)
        if unknown:
            raise ValueError(f"{file}: unknown config key(s): {', '.join(sorted(unknown))}")
        config.update(data)
    return config


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def build_scheduler(config: dict | None = None) -> Scheduler:
    """Build a scheduler from a config dict (see `load_config`); defaults when omitted."""
    config = config if config is not None else DEFAULT_CONFIG
    users = UserManagement()
    for user in config["users"]:
        users.add_user(user["name"], user["quota"])

    registry = ExecutorRegistry(
        [SyncExecutor(), BackupExecutor(), DeleteExecutor(config["delete_allowed_prefixes"])]
    )
    scheduler = Scheduler(users=users, executors=registry)
    for task in config["tasks"]:
        scheduler.add_task(task["user"], task["at"], task["action"], task["target"])
    return scheduler


def main() -> None:
    config = load_config()
    setup_logging(config["log_level"])
    scheduler = build_scheduler(config)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(scheduler.run_forever(config["poll_interval"]))


if __name__ == "__main__":
    main()
