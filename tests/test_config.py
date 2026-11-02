import json

import pytest

from main import CONFIG_ENV, DEFAULT_CONFIG, build_scheduler, load_config


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Run each test in an empty directory without $SCHEDULER_CONFIG."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(CONFIG_ENV, raising=False)


def write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_defaults_when_no_file():
    assert load_config() == DEFAULT_CONFIG


def test_returns_a_copy_of_defaults():
    load_config()["users"].append({"name": "x", "quota": 1})
    assert len(DEFAULT_CONFIG["users"]) == 2


def test_reads_config_json_in_cwd(tmp_path):
    write(tmp_path / "config.json", {"log_level": "DEBUG"})
    config = load_config()
    assert config["log_level"] == "DEBUG"
    assert config["poll_interval"] == DEFAULT_CONFIG["poll_interval"]  # unset keys keep defaults


def test_explicit_path(tmp_path):
    path = write(tmp_path / "custom.json", {"poll_interval": 5})
    assert load_config(path)["poll_interval"] == 5


def test_env_var_path(tmp_path, monkeypatch):
    path = write(tmp_path / "env.json", {"poll_interval": 7})
    monkeypatch.setenv(CONFIG_ENV, str(path))
    assert load_config()["poll_interval"] == 7


def test_explicit_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "missing.json")


def test_unknown_key_rejected(tmp_path):
    path = write(tmp_path / "bad.json", {"pol_interval": 5})
    with pytest.raises(ValueError, match="pol_interval"):
        load_config(path)


def test_non_object_rejected(tmp_path):
    path = write(tmp_path / "bad.json", [1, 2])
    with pytest.raises(ValueError, match="JSON object"):
        load_config(path)


def test_build_scheduler_from_config():
    config = load_config() | {
        "delete_allowed_prefixes": ["/var/cache/"],
        "users": [{"name": "carol", "quota": 2}],
        "tasks": [{"user": "carol", "at": "08:30", "action": "delete", "target": "/var/cache/a"}],
    }
    scheduler = build_scheduler(config)
    assert [u.name for u in scheduler.users] == ["carol"]
    assert [(t.user, f"{t.time:%H:%M}", t.target) for t in scheduler.tasks] == [("carol", "08:30", "/var/cache/a")]
    assert scheduler.executors.get("delete").allowed_prefixes == ("/var/cache/",)


def test_project_config_json_is_valid():
    from pathlib import Path

    config = load_config(Path(__file__).parent.parent / "config.json")
    build_scheduler(config)
