import datetime

import pytest

from main import InMemoryTaskRepository, InMemoryUserRepository, Task, TaskRepository, User, UserRepository


@pytest.mark.parametrize("cls", [UserRepository, TaskRepository])
def test_repositories_are_abstract(cls):
    with pytest.raises(TypeError):
        cls()


class TestInMemoryUserRepository:
    def test_save_get_delete(self):
        repo = InMemoryUserRepository()
        user = User(name="alice", quota=1)
        repo.save(user)
        assert repo.get("alice") is user
        assert repo.all() == [user]
        repo.delete("alice")
        assert repo.get("alice") is None
        assert repo.all() == []

    def test_save_overwrites(self):
        repo = InMemoryUserRepository()
        repo.save(User(name="alice", quota=1))
        repo.save(User(name="alice", quota=5))
        assert repo.get("alice").quota == 5
        assert len(repo.all()) == 1

    def test_delete_missing_is_noop(self):
        InMemoryUserRepository().delete("ghost")

    def test_all_returns_copy(self):
        repo = InMemoryUserRepository()
        repo.all().append(User(name="x", quota=1))
        assert repo.all() == []


class TestInMemoryTaskRepository:
    def make(self, task_id: int) -> Task:
        return Task(user="alice", time=datetime.time(12, 0), action="sync", target="/a", id=task_id)

    def test_next_id_increments(self):
        repo = InMemoryTaskRepository()
        assert [repo.next_id() for _ in range(3)] == [1, 2, 3]

    def test_save_get_delete(self):
        repo = InMemoryTaskRepository()
        task = self.make(repo.next_id())
        repo.save(task)
        assert repo.get(task.id) is task
        repo.delete(task.id)
        assert repo.get(task.id) is None

    def test_all_keeps_insertion_order(self):
        repo = InMemoryTaskRepository()
        tasks = [self.make(i) for i in (3, 1, 2)]
        for task in tasks:
            repo.save(task)
        assert repo.all() == tasks
