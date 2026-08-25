.PHONY: setup deps models api dashboard test evaluate benchmark demo down

setup:
	python -m venv .venv
	.venv/bin/python -m pip install -e ".[dev]"
	cd dashboard && npm install

deps:
	docker compose up -d --wait qdrant

models:
	ollama pull nomic-embed-text
	ollama pull qwen2.5-coder:1.5b

api:
	.venv/bin/repomesh serve

dashboard:
	cd dashboard && npm run dev

test:
	.venv/bin/python -m pytest
	.venv/bin/ruff check src tests evaluation benchmarks
	cd dashboard && npm test && npm run build

evaluate:
	.venv/bin/python -m evaluation.run

benchmark:
	.venv/bin/python -m benchmarks.run

demo:
	bash scripts/demo.sh

down:
	docker compose down

