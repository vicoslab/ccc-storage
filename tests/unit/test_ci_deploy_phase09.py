from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_workflow_has_always_on_and_conditional_lanes():
    text = CI.read_text()

    for job in ("lint:", "unit:", "fuse-unpriv:", "multinode:", "bench-smoke:"):
        assert job in text
    for job in ("kernel-mount:", "docker-propagation:", "real-s3:"):
        assert job in text
    assert "continue-on-error: true" in text
    assert "skip with reason" in text.lower()
    assert "CCC_TEST_ROOT" in text
    assert "CAPS.squashfuse" not in text
    assert "CAPS.unpriv_fuse" in text
    assert "find tests/bench -name 'test_*.py'" in text


def test_ci_enforces_core_pack_coverage_gate():
    text = CI.read_text()
    assert "--cov=src/ccc_layered_core" in text
    assert "--cov=src/ccc_layered_pack" in text
    assert "--cov-fail-under=85" in text


def test_deploy_artifacts_exist_and_are_safe_defaults():
    service = ROOT / "deploy" / "ccc-layered-mountd.service"
    install = ROOT / "deploy" / "install.sh"
    uninstall = ROOT / "deploy" / "uninstall.sh"
    prereqs = ROOT / "deploy" / "PREREQS.md"
    smoke = ROOT / "deploy" / "runtime-smoke.sh"
    fuse_smoke = ROOT / "deploy" / "fuse-smoke.sh"
    docker_smoke = ROOT / "deploy" / "docker-smoke.sh"
    s3_smoke = ROOT / "deploy" / "s3-smoke.sh"

    for path in (
        service,
        install,
        uninstall,
        prereqs,
        smoke,
        fuse_smoke,
        docker_smoke,
        s3_smoke,
    ):
        assert path.exists(), path

    service_text = service.read_text()
    assert "ExecStart=" in service_text
    assert "ccc-layered-mountd" in service_text
    assert "Restart=on-failure" in service_text
    assert "CAP_SYS_ADMIN" in service_text
    assert "/dev/fuse" in service_text
    assert "CCC_NFS_ROOT" in service_text
    assert "CCC_MANAGED_PARENT" in service_text
    assert "CCC_MANAGED_PARENTS" not in service_text

    install_text = install.read_text()
    assert "systemctl daemon-reload" in install_text
    assert "systemctl enable" not in install_text  # install is copy-only by default

    prereq_text = prereqs.read_text().lower()
    for phrase in ("/dev/fuse", "fusermount3", "squashfs", "overlay", "nfs"):
        assert phrase in prereq_text
    assert "runtime-smoke.sh" in prereq_text
    assert "fuse-smoke.sh" in prereq_text
    assert "docker-smoke.sh" in prereq_text
    assert "s3-smoke.sh" in prereq_text
    assert "ceph-7.fri.uni-lj.si" in prereq_text
    assert "ccc_allow_fuse_skip" in prereq_text

    smoke_text = smoke.read_text()
    assert "/storage/.ccc-layered" not in smoke_text
    assert "ccc_layered_mountd.daemon" in smoke_text
    assert "ccc_layered_cli.main" in smoke_text

    fuse_text = fuse_smoke.read_text()
    assert "/storage/.ccc-layered" not in fuse_text
    assert "ccc_layered_pack.cli build" in fuse_text
    assert "ccc_layered_pack.cli verify" in fuse_text
    assert "unsquashfs" in fuse_text
    assert "squashfuse" in fuse_text
    assert "CCC_ALLOW_FUSE_SKIP" in fuse_text
    assert "ccc-fuse-sidecar" in fuse_text or "fusermount3 sidecar" in fuse_text
    assert ".scratch/ccc-layered-fuse-smoke" in fuse_text
    assert "skip with reason" in fuse_text

    docker_text = docker_smoke.read_text()
    assert "/storage/.ccc-layered" not in docker_text
    assert "docker" in docker_text
    assert "--device /dev/fuse" in docker_text
    assert "--cap-add SYS_ADMIN" in docker_text
    assert "--security-opt apparmor=unconfined" in docker_text
    assert "deploy/runtime-smoke.sh" in docker_text
    assert "deploy/fuse-smoke.sh" in docker_text


def test_dockerfile_is_optional_test_image_only():
    text = (ROOT / "Dockerfile").read_text()
    assert "ccc-layered-storage" in text
    assert "make test" in text
    assert ".[dev,manifest,s3]" in text
    assert "COPY deploy ./deploy" in text
    assert "COPY .github ./.github" in text
    assert "Dockerfile ./" in text
    assert "squashfs-tools" in text
    assert "squashfuse" in text
    assert "fuse3" in text
    assert "make" in text
    assert "production" not in text.lower()
