from pathlib import Path
import subprocess
import sys

import pytest

from render import Deployment


@pytest.fixture
def deployment() -> Deployment:
    return Deployment("example-books", "books.example.com")


@pytest.mark.parametrize("invalid", ["host\nExecStart=bad", "host:/", "$(cmd)", "-host", "https://host"])
def test_rejects_config_injection(invalid: str) -> None:
    with pytest.raises(ValueError):
        Deployment(invalid, "books.example.com")


@pytest.mark.parametrize("state", [
    '{"filesystems": []}',
    '{"filesystems": [{"source": "/dev/vda3", "fstype": "ext4"}]}',
    '{"filesystems": [{"source": "wrong-bucket:/audiobookshelf", "fstype": "fuse.ossfs"}]}',
])
def test_guard_rejects_absent_local_or_wrong_storage(tmp_path: Path, deployment: Deployment, state: str) -> None:
    fake = tmp_path / "findmnt"
    fake.write_text(f"#!/bin/sh\nprintf '%s' '{state}'\n")
    fake.chmod(0o755)
    guard = tmp_path / "guard.py"
    guard.write_text(deployment.files()["guard.py"])
    result = subprocess.run([sys.executable, str(guard)], env={"PATH": str(tmp_path)}, capture_output=True, text=True)
    assert result.returncode != 0
    assert "Expected OSS is not mounted" in result.stderr


def test_container_cannot_restart_without_mount_dependency(deployment: Deployment) -> None:
    files = deployment.files()
    assert 'restart: "no"' in files["docker-compose.yml"]
    assert files["docker-compose.yml"].count("create_host_path: false") == 2
    assert "BindsTo=audiobookshelf-storage.service" in files["audiobookshelf.service"]
    assert "ExecStartPre=" in files["audiobookshelf.service"]
    assert "ensure_diskfree=5120" in files["audiobookshelf-storage.service"]


def test_guard_accepts_the_configured_oss_mount(tmp_path: Path, deployment: Deployment) -> None:
    fake = tmp_path / "findmnt"
    fake.write_text('#!/bin/sh\nprintf \'%s\' \'{"filesystems":[{"source":"example-books:/audiobookshelf","fstype":"fuse.ossfs"}]}\'\n')
    fake.chmod(0o755)
    guard = tmp_path / "guard.py"
    guard.write_text(deployment.files()["guard.py"])
    result = subprocess.run([sys.executable, str(guard), "--mount-only"], env={"PATH": str(tmp_path)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
