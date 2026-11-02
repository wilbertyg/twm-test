"""HTTP API (FastAPI) for the scheduler in `main.py`.

Run:
    uv run fastapi dev api.py     # development, auto-reload
    uv run fastapi run api.py     # production

Interactive docs: http://127.0.0.1:8000/docs
Settings come from `config.json` (or the file named by $SCHEDULER_CONFIG).
"""

import asyncio
import contextlib
import datetime
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from main import (
    Scheduler,
    SchedulerError,
    Task,
    TaskNotFoundError,
    UnknownActionError,
    User,
    UserAlreadyExistsError,
    UserHasTasksError,
    UserNotFoundError,
    build_scheduler,
    load_config,
    setup_logging,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64, examples=["carol"])
    quota: int = Field(ge=0, examples=[3])


class UserOut(BaseModel):
    name: str
    quota: int
    executed: int
    remaining: int

    @classmethod
    def from_user(cls, user: User) -> "UserOut":
        return cls(name=user.name, quota=user.quota, executed=user.executed, remaining=user.remaining)


class TaskCreate(BaseModel):
    user: str = Field(examples=["alice"])
    at: str = Field(pattern=r"^\d{2}:\d{2}$", description="Daily time, HH:MM", examples=["12:00"])
    action: str = Field(examples=["sync"])
    target: str = Field(min_length=1, examples=["/data/x"])


class TaskOut(BaseModel):
    id: int
    user: str
    at: str
    action: str
    target: str
    last_run: datetime.date | None

    @classmethod
    def from_task(cls, task: Task) -> "TaskOut":
        return cls(
            id=task.id,
            user=task.user,
            at=task.time.strftime("%H:%M"),
            action=task.action,
            target=task.target,
            last_run=task.last_run,
        )


class RunRequest(BaseModel):
    now: datetime.datetime | None = Field(
        default=None, description="Simulated current time; defaults to the server's local time"
    )


class RunResult(BaseModel):
    executed: list[TaskOut]


# --------------------------------------------------------------------------- #
# Service: serialises access between requests and the background poller
# --------------------------------------------------------------------------- #

class SchedulerService:
    def __init__(self, scheduler: Scheduler) -> None:
        self.scheduler = scheduler
        self._lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def locked(self) -> AsyncIterator[Scheduler]:
        async with self._lock:
            yield self.scheduler

    async def run_pending(self, now: datetime.datetime | None = None) -> list[Task]:
        async with self._lock:
            return await self.scheduler.run_pending(now)


async def _poll(service: SchedulerService, interval: float, stop: asyncio.Event) -> None:
    log.info("Background scheduler started (every %ss)", interval)
    while not stop.is_set():
        try:
            await service.run_pending()
        except Exception:
            log.exception("Scheduler tick failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
    log.info("Background scheduler stopped")


def get_service(request: Request) -> SchedulerService:
    return request.app.state.service


ServiceDep = Annotated[SchedulerService, Depends(get_service)]


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

router = APIRouter()


@router.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/actions", tags=["meta"])
async def list_actions(service: ServiceDep) -> list[str]:
    async with service.locked() as scheduler:
        return list(scheduler.executors.actions)


@router.get("/users", tags=["users"])
async def list_users(service: ServiceDep) -> list[UserOut]:
    async with service.locked() as scheduler:
        return [UserOut.from_user(u) for u in scheduler.users]


@router.post("/users", status_code=status.HTTP_201_CREATED, tags=["users"])
async def create_user(body: UserCreate, service: ServiceDep) -> UserOut:
    async with service.locked() as scheduler:
        return UserOut.from_user(scheduler.users.add_user(body.name, body.quota))


@router.post("/users/reset-quotas", tags=["users"])
async def reset_quotas(service: ServiceDep) -> list[UserOut]:
    async with service.locked() as scheduler:
        scheduler.users.reset_quotas()
        return [UserOut.from_user(u) for u in scheduler.users]


@router.get("/users/{name}", tags=["users"])
async def get_user(name: str, service: ServiceDep) -> UserOut:
    async with service.locked() as scheduler:
        return UserOut.from_user(scheduler.users.get_user(name))


@router.delete("/users/{name}", status_code=status.HTTP_204_NO_CONTENT, tags=["users"])
async def delete_user(name: str, service: ServiceDep) -> Response:
    async with service.locked() as scheduler:
        scheduler.remove_user(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/tasks", tags=["tasks"])
async def list_tasks(service: ServiceDep, user: str | None = None) -> list[TaskOut]:
    async with service.locked() as scheduler:
        tasks = scheduler.tasks if user is None else scheduler.tasks.for_user(user)
        return [TaskOut.from_task(t) for t in tasks]


@router.post("/tasks", status_code=status.HTTP_201_CREATED, tags=["tasks"])
async def create_task(body: TaskCreate, service: ServiceDep) -> TaskOut:
    async with service.locked() as scheduler:
        try:
            task = scheduler.add_task(body.user, body.at, body.action, body.target)
        except SchedulerError:
            raise
        except ValueError:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Invalid time: {body.at}") from None
        return TaskOut.from_task(task)


@router.get("/tasks/{task_id}", tags=["tasks"])
async def get_task(task_id: int, service: ServiceDep) -> TaskOut:
    async with service.locked() as scheduler:
        return TaskOut.from_task(scheduler.get_task(task_id))


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["tasks"])
async def delete_task(task_id: int, service: ServiceDep) -> Response:
    async with service.locked() as scheduler:
        scheduler.remove_task(task_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/run", tags=["scheduler"])
async def run_pending(service: ServiceDep, body: RunRequest | None = None) -> RunResult:
    """Run every task due at `now` (default: server time) right away."""
    executed = await service.run_pending(body.now if body else None)
    return RunResult(executed=[TaskOut.from_task(t) for t in executed])


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #

_ERROR_STATUS: dict[type[SchedulerError], int] = {
    UserNotFoundError: status.HTTP_404_NOT_FOUND,
    TaskNotFoundError: status.HTTP_404_NOT_FOUND,
    UserAlreadyExistsError: status.HTTP_409_CONFLICT,
    UserHasTasksError: status.HTTP_409_CONFLICT,
    UnknownActionError: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


async def _scheduler_error_handler(request: Request, exc: Exception) -> JSONResponse:
    code = next((c for t, c in _ERROR_STATUS.items() if isinstance(exc, t)), status.HTTP_400_BAD_REQUEST)
    return JSONResponse(status_code=code, content={"detail": str(exc)})


async def _log_requests(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000
    log.info("%s %s -> %d (%.1f ms)", request.method, request.url.path, response.status_code, elapsed_ms)
    return response


def create_app(scheduler: Scheduler | None = None, *, poll_interval: float | None = 30.0) -> FastAPI:
    """Build the app. `poll_interval=None` (or 0) disables the background scheduler loop."""
    service = SchedulerService(scheduler if scheduler is not None else build_scheduler())

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        poller = asyncio.create_task(_poll(service, poll_interval, stop)) if poll_interval else None
        try:
            yield
        finally:
            stop.set()
            if poller is not None:
                await poller

    app = FastAPI(title="twm-test scheduler", version="0.1.0", lifespan=lifespan)
    app.state.service = service
    app.add_exception_handler(SchedulerError, _scheduler_error_handler)
    app.middleware("http")(_log_requests)
    app.include_router(router)
    return app


config = load_config()
setup_logging(config["log_level"])
app = create_app(build_scheduler(config), poll_interval=config["poll_interval"])
