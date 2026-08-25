# Third-party notices

RepoMesh application code was implemented independently. No source code was copied or adapted from:

- `denfry/codebase-index`
- `Neverdecel/CodeRAG`
- `Zakeertech3/code-rag-engine`

Those repositories were supplied as possible architectural references only. Consequently there are no reused files requiring carried copyright headers or source-license notices.

RepoMesh depends on separately distributed open-source packages and services, including FastAPI, Pydantic, Uvicorn, HTTPX, SQLite, Qdrant/qdrant-client, Tree-sitter, tree-sitter-language-pack, pathspec, prometheus-client, structlog, pytest, Ruff, mypy, TypeScript, Vite, Vitest, and Ollama. Their packages, container images, model artifacts, copyright notices, and license texts remain governed by their respective upstream terms. Python dependency versions are declared in `pyproject.toml`, frontend versions in `dashboard/package-lock.json`, and container versions in `compose.yaml`.

The Ollama models are external artifacts and are not redistributed by this repository. Users should review model-specific notices shown by their model source.

RepoMesh's sample ParcelFlow repository and evaluation cases are original test fixtures created for this project.
