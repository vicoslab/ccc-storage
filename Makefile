# ccc-layered-storage — developer loop.
#
# Nothing here requires Docker or privilege. FUSE/kernel/multinode tiers are
# capability-gated by the probe in tests/fakes/capability.py and skip with a
# clear reason when unavailable.

PYTHON ?= python
PYTEST ?= $(PYTHON) -m pytest

.PHONY: help env env-update lint fmt test test-fuse test-multinode test-all bench probe clean

help:
	@echo "Targets:"
	@echo "  env           create the ccc-dev conda env from environment.yml"
	@echo "  env-update    update the ccc-dev conda env in place"
	@echo "  lint          ruff check + mypy (src)"
	@echo "  fmt           ruff format + ruff --fix"
	@echo "  test          unit tier only (fast, no FUSE, no privilege)"
	@echo "  test-fuse     unit + unprivileged FUSE tier"
	@echo "  test-multinode unit + multinode tier"
	@echo "  test-all      everything the capability probe allows"
	@echo "  bench         performance benchmarks (smoke sizes)"
	@echo "  probe         print the capability probe result"
	@echo "  clean         remove caches and the .scratch test root"

env:
	mamba env create -f environment.yml || conda env create -f environment.yml

env-update:
	mamba env update -f environment.yml --prune || conda env update -f environment.yml --prune

lint:
	$(PYTHON) -m ruff check src tests
	$(PYTHON) -m mypy src

fmt:
	$(PYTHON) -m ruff format src tests
	$(PYTHON) -m ruff check --fix src tests

test:
	$(PYTEST) tests/unit -q

test-fuse:
	$(PYTEST) tests/unit tests/fuse -q -m "not kernel_mount and not docker"

test-multinode:
	$(PYTEST) tests/unit tests/multinode -q

test-all:
	$(PYTEST) tests -q -m "not docker"

bench:
	$(PYTEST) tests/bench -q -m bench

probe:
	$(PYTHON) -c "from tests.fakes.capability import CAPS; import dataclasses, json; print(json.dumps(dataclasses.asdict(CAPS), indent=2))"

clean:
	rm -rf .scratch
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name '*.egg-info' -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache
