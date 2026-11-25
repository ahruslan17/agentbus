.PHONY: check fmt test db-up db-down migrate api worker bridge smoke smoke-pool prune

KEEP ?= 50
PRUNE_ARGS ?=

check:
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run pytest

fmt:
	uv run ruff format src tests
	uv run ruff check --fix src tests

test:
	uv run pytest

db-up:
	docker compose up -d db

db-down:
	docker compose down

migrate:
	uv run python -m agentbus.migrate

api:
	uv run uvicorn agentbus.api:build_app --factory --host $${AGENTBUS_BIND_HOST:-127.0.0.1} --port $${AGENTBUS_BIND_PORT:-8787}

worker:
	uv run python -m agentbus.worker

bridge:
	uv run python -m agentbus.bridge

smoke:
	./tests/smoke.sh

smoke-pool:
	./tests/smoke_pool.sh

prune:
	uv run python -m agentbus.prune --keep $(KEEP) $(PRUNE_ARGS)
