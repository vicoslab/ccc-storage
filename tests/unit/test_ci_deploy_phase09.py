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
    deploy_readme = ROOT / "deploy" / "README.md"
    service = ROOT / "deploy" / "systemd" / "ccc-storage-mountd.service"
    install = ROOT / "deploy" / "systemd" / "install.sh"
    uninstall = ROOT / "deploy" / "systemd" / "uninstall.sh"
    prereqs = ROOT / "dev" / "docs" / "operations" / "node-prerequisites.md"
    smoke = ROOT / "dev" / "validation" / "local" / "runtime-smoke.sh"
    fuse_smoke = ROOT / "dev" / "validation" / "local" / "fuse-smoke.sh"
    docker_smoke = ROOT / "dev" / "validation" / "local" / "docker-smoke.sh"
    s3_smoke = ROOT / "dev" / "validation" / "s3" / "s3-smoke.sh"
    s3_cold_hpc_smoke = ROOT / "dev" / "validation" / "s3" / "s3-cold-hpc-smoke.sh"
    nested_smoke = ROOT / "dev" / "validation" / "docker" / "nested-runtime-smoke.sh"
    observation_smoke = ROOT / "dev" / "validation" / "docker" / "observation-runtime-smoke.sh"

    for path in (
        deploy_readme,
        service,
        install,
        uninstall,
        prereqs,
        smoke,
        fuse_smoke,
        docker_smoke,
        s3_smoke,
        s3_cold_hpc_smoke,
        nested_smoke,
        observation_smoke,
    ):
        assert path.exists(), path

    dev_readme = ROOT / "dev" / "README.md"
    deploy_text = deploy_readme.read_text()
    dev_text = dev_readme.read_text()
    assert "deploy/docker/mountd.Dockerfile" in deploy_text
    assert "Development-only validation" in deploy_text
    assert "dev/validation/performance/performance-runtime-benchmark.sh" not in deploy_text
    assert "dev/validation/performance/performance-runtime-benchmark.sh" in dev_text

    service_text = service.read_text()
    assert "ExecStart=" in service_text
    assert "ccc-storage mountd" in service_text
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
    assert "s3-cold-hpc-smoke.sh" in prereq_text
    assert "nested-runtime-smoke.sh" in prereq_text
    assert "observation-runtime-smoke.sh" in prereq_text
    assert "ccc_layered_observe" in prereq_text
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
    assert "dev/validation/local/runtime-smoke.sh" in docker_text
    assert "dev/validation/local/fuse-smoke.sh" in docker_text


def test_dockerfile_is_optional_test_image_only():
    assert not (ROOT / "Dockerfile").exists()
    dockerfile = ROOT / "dev" / "docker" / "test.Dockerfile"
    text = dockerfile.read_text()
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert "ccc-layered-storage" in text
    assert "make test" in text
    assert ".[dev,manifest,s3,fuse]" in text
    assert "pyfuse3" in pyproject
    assert "trio" in pyproject
    assert "COPY deploy ./deploy" in text
    assert "COPY dev ./dev" in text
    assert "COPY .github ./.github" in text
    assert "COPY pyproject.toml README.md ./" in text
    assert "squashfs-tools" in text
    assert "squashfuse" in text
    assert "fuse3" in text
    assert "libfuse3-dev" in text
    assert "pkg-config" in text
    assert "make" in text
    assert "production" not in text.lower()
