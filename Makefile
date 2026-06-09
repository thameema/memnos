# memnos — developer convenience targets.
# Postgres is a prerequisite (external). For local dev, the pgvector container is easiest:
#   docker compose -f docker-compose.poc.yml up -d
#
# The test/serve targets read MEMNOS_DSN / MEMNOS_URL / MEMNOS_SECRET_KEY from the
# environment (or a repo-root .env) — the same vars the server uses.

PY ?= python
MEMNOS_URL ?= http://127.0.0.1:8900

.PHONY: help install test serve fmt clean

help:
	@echo "make install   # pipx-install the 'memnos' CLI + server from this checkout"
	@echo "make serve     # run the server (foreground) on $(MEMNOS_URL)"
	@echo "make test      # run the full tests/ suite against a running server"
	@echo "make clean     # remove build artifacts + __pycache__"

install:
	pipx install . --force

serve:
	$(PY) memnos_server.py

# Runs every tests/test_*.py from the repo root; exits non-zero if any fail.
test:
	@fail=0; pass=0; \
	for t in tests/test_*.py; do \
	  if $(PY) "$$t" >/tmp/memnos_test.out 2>&1; then \
	    pass=$$((pass+1)); echo "PASS  $$t"; \
	  else \
	    fail=$$((fail+1)); echo "FAIL  $$t"; tail -5 /tmp/memnos_test.out; \
	  fi; \
	done; \
	echo "----- $$pass passed, $$fail failed -----"; \
	[ $$fail -eq 0 ]

clean:
	rm -rf build dist *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
