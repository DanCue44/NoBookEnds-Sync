PY ?= .venv/bin/python

.PHONY: venv test run fixture image image-cpu

venv:
	python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

test:
	$(PY) -m pytest

run:
	DATA_DIR=./data $(PY) -m nobookends_sync

fixture:
	$(PY) tests/fixtures/build_fixture.py

image:
	docker build --force-rm -t nobookends-sync:dev .

image-cpu:
	docker build --force-rm -f Dockerfile.cpu -t nobookends-sync:cpu .
