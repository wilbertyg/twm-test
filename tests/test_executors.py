import datetime
from asyncio import run
import logging

import pytest

from main import (
    BackupExecutor,
    DeleteExecutor,
    ExecutorRegistry,
    SchedulerError,
    SyncExecutor,
    Task,
    TaskExecutor,
    UnknownActionError,
)


def make_task(action: str, target: str) -> Task:
    return Task(user="alice", time=datetime.time(12, 0), action=action, target=target)


def test_base_executor_is_abstract():
    with pytest.raises(TypeError):
        TaskExecutor()


def test_repr():
    assert repr(SyncExecutor()) == "SyncExecutor(action='sync')"


class TestBuiltInExecutors:
    @pytest.mark.parametrize(
        "executor, message",
        [(SyncExecutor(), "Syncing /data/x for alice"), (BackupExecutor(), "Backing up /data/x for alice")],
    )
    def test_logs_action(self, executor, message, caplog):
        with caplog.at_level(logging.INFO):
            run(executor.execute(make_task(executor.action, "/data/x")))
        assert message in caplog.text

    def test_delete_allowed_under_tmp(self, caplog):
        with caplog.at_level(logging.INFO):
            run(DeleteExecutor().execute(make_task("delete", "/tmp/z")))
        assert "Deleting /tmp/z for alice" in caplog.text

    @pytest.mark.parametrize("target", ["/data/x", "/", "/tmpfoo", "tmp/z"])
    def test_delete_refused_outside_allowed_prefixes(self, target):
        with pytest.raises(PermissionError):
            run(DeleteExecutor().execute(make_task("delete", target)))

    def test_delete_custom_prefixes(self):
        executor = DeleteExecutor(allowed_prefixes=["/var/cache/"])
        assert executor.allowed_prefixes == ("/var/cache/",)
        run(executor.execute(make_task("delete", "/var/cache/a")))
        with pytest.raises(PermissionError):
            run(executor.execute(make_task("delete", "/tmp/z")))

    def test_delete_prefixes_are_read_only(self):
        with pytest.raises(AttributeError):
            DeleteExecutor().allowed_prefixes = ("/",)


class TestExecutorRegistry:
    def test_with_defaults(self):
        registry = ExecutorRegistry.with_defaults()
        assert registry.actions == ("backup", "delete", "sync")
        assert isinstance(registry.get("sync"), SyncExecutor)
        assert isinstance(registry.get("backup"), BackupExecutor)
        assert isinstance(registry.get("delete"), DeleteExecutor)

    def test_starts_empty(self):
        registry = ExecutorRegistry()
        assert registry.actions == ()
        with pytest.raises(UnknownActionError):
            registry.get("sync")

    def test_constructor_registers_executors(self, recorder):
        registry = ExecutorRegistry([recorder])
        assert "record" in registry
        assert "sync" not in registry

    def test_unknown_action_raises(self):
        with pytest.raises(UnknownActionError) as exc:
            ExecutorRegistry.with_defaults().get("format")
        assert isinstance(exc.value, SchedulerError)
        assert "format" in str(exc.value)

    def test_register_custom_executor(self, recorder):
        registry = ExecutorRegistry()
        registry.register(recorder)
        assert registry.get("record") is recorder

    def test_register_replaces_same_action(self):
        registry = ExecutorRegistry.with_defaults()
        strict = DeleteExecutor(allowed_prefixes=())
        registry.register(strict)
        assert registry.get("delete") is strict

    def test_register_without_action_rejected(self):
        class Nameless(TaskExecutor):
            def execute(self, task):
                pass

        with pytest.raises(ValueError):
            ExecutorRegistry().register(Nameless())
