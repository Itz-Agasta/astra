.PHONY: sync lint format typecheck check run-harness run-mcp dev-client clean

sync:
	uv sync --all-packages
	bun install

lint:
	uv run ruff check .

format:
	uv run ruff format .

typecheck-py:
	uv run ty check .

typecheck-web:
	cd client && bun run check-types

check: lint typecheck-py
	uv run ruff format --check .
	cd client && bun run check-types
	bun run check

run-harness:
	uv run -m harness.main

run-mcp:
	uv run -m mcp.main

dev-client:
	bun run dev:client

clean:
	find . -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null
	find . -type d -name '*.egg-info' -exec rm -rf {} + 2>/dev/null
	rm -rf .ruff_cache/ .ty_cache/
	rm -rf client/.next/ client/.turbo/ client/node_modules
	rm -rf .turbo/ node_modules/ .venv/
	find . -name '*.pyc' -delete
