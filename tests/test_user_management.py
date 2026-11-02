import pytest

from main import (
    InMemoryUserRepository,
    QuotaReachedError,
    SchedulerError,
    User,
    UserAlreadyExistsError,
    UserManagement,
    UserNotFoundError,
)


def test_add_user_returns_and_stores_user():
    users = UserManagement()
    user = users.add_user("alice", quota=3)
    assert user == User(name="alice", quota=3)
    assert users.get_user("alice") is user


def test_add_duplicate_user_raises():
    users = UserManagement()
    users.add_user("alice", quota=3)
    with pytest.raises(UserAlreadyExistsError) as exc:
        users.add_user("alice", quota=5)
    assert isinstance(exc.value, ValueError)  # backwards compatible
    assert users.get_user("alice").quota == 3


def test_add_user_with_negative_quota_raises():
    with pytest.raises(ValueError):
        UserManagement().add_user("alice", quota=-1)


def test_get_unknown_user_raises():
    with pytest.raises(UserNotFoundError) as exc:
        UserManagement().get_user("ghost")
    assert isinstance(exc.value, SchedulerError)
    assert "ghost" in str(exc.value)


def test_remove_user(users):
    users.remove_user("alice")
    with pytest.raises(UserNotFoundError):
        users.get_user("alice")
    assert users.get_user("bob")


def test_remove_unknown_user_raises(users):
    with pytest.raises(UserNotFoundError):
        users.remove_user("ghost")


def test_container_protocol(users):
    assert [u.name for u in users] == ["alice", "bob"]
    assert len(users) == 2
    assert "alice" in users
    assert "ghost" not in users
    assert 42 not in users


def test_record_execution(users):
    bob = users.get_user("bob")
    users.record_execution(bob)
    assert bob.executed == 1
    with pytest.raises(QuotaReachedError):
        users.record_execution(bob)


def test_reset_quotas(users):
    users.record_execution(users.get_user("alice"))
    users.record_execution(users.get_user("bob"))
    users.reset_quotas()
    assert all(u.executed == 0 for u in users)


def test_uses_injected_repository():
    repo = InMemoryUserRepository()
    UserManagement(repo).add_user("alice", quota=1)
    assert repo.get("alice") is not None


def test_saves_through_repository_on_every_change():
    class SpyRepository(InMemoryUserRepository):
        def __init__(self):
            super().__init__()
            self.saves = 0

        def save(self, user):
            self.saves += 1
            super().save(user)

    repo = SpyRepository()
    users = UserManagement(repo)
    alice = users.add_user("alice", quota=2)
    users.record_execution(alice)
    users.reset_quotas()
    assert repo.saves == 3
