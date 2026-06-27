from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOST_SCRIPT = ROOT / "deploy" / "privileged-runtime-smoke.sh"
CONTAINER_SCRIPT = ROOT / "deploy" / "privileged-runtime-container.sh"


def _text() -> str:
    return HOST_SCRIPT.read_text() + "\n" + CONTAINER_SCRIPT.read_text()


def test_privileged_runtime_smoke_scripts_exist_and_are_executable():
    for path in (HOST_SCRIPT, CONTAINER_SCRIPT):
        assert path.exists(), path
        assert os.access(path, os.X_OK), path


def test_host_script_uses_privileged_docker_fuse_and_shared_propagation():
    text = HOST_SCRIPT.read_text()
    for phrase in (
        "--privileged",
        "/dev/fuse:/dev/fuse:rwm",
        "apparmor=unconfined",
        "seccomp=unconfined",
        "bind-propagation=rshared",
    ):
        assert phrase in text


def test_scripts_document_no_sidecar_without_using_sidecar_paths():
    text = _text()
    assert "no sidecar" in text.lower()

    for line in text.splitlines():
        if "ccc-fuse-sidecar" in line or "/run/ccc-fuse-sidecar" in line:
            lowered = line.lower()
            assert "no sidecar" in lowered or "no ccc-fuse-sidecar" in lowered


def test_runtime_root_safety_terms_are_present():
    text = HOST_SCRIPT.read_text()
    for phrase in (
        "/storage/user",
        "/storage/datasets",
        "/storage/group",
        "/storage",
        "/home",
        "/tmp/*",
        ".scratch/*",
    ):
        assert phrase in text
    assert "refusing unsafe CCC_RUNTIME_ROOT" in text


def test_client_container_checks_use_docker_exec_and_default_container():
    text = HOST_SCRIPT.read_text()
    assert "CCC_CLIENT_CONTAINERS:-domen-cuda10" in text
    assert "docker_bin\" exec -i \"$container_name\"" in text
    assert "client_exec_script" in text
    assert "command -v python3" in text
    assert "client-writes" in text


def test_writable_overlay_is_sealed_before_commit_and_metadata_is_escaped():
    host_text = HOST_SCRIPT.read_text()
    container_text = CONTAINER_SCRIPT.read_text()

    assert "request_container_seal \"$container_name\"" in host_text
    assert "touch /ccc-runtime/control/seal" in host_text
    assert "test -e /ccc-runtime/control/sealed" in host_text
    assert "cleanup_mounts" in container_text
    assert "mv \"$control_dir/seal\" \"$control_dir/sealed\"" in container_text
    assert "printf 'CHILD_ID=%q\\n'" in container_text
    assert "mount --make-rshared \"$runtime_root\"" in container_text
    assert "runtime root is not shared inside privileged container" in container_text


def test_container_exercises_current_runtime_data_plane():
    text = _text()
    for phrase in (
        "fuse-overlayfs",
        "ccc-layered-mountd",
        "ccc-layered commit",
        "ccc-pack build",
        "ccc-layered mount",
        "ccc-layered doctor",
    ):
        assert phrase in text
