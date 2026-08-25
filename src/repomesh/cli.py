from __future__ import annotations

import argparse

import uvicorn

from repomesh.api import create_app
from repomesh.config import Settings
from repomesh.models import SearchMode
from repomesh.repositories import register_repository, validate_repository_path
from repomesh.services import Services


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="repomesh")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    register = commands.add_parser("register")
    register.add_argument("path")
    index = commands.add_parser("index")
    index.add_argument("repository_id")
    index.add_argument("--mode", choices=["full", "incremental"], default="incremental")
    search = commands.add_parser("search")
    search.add_argument("repository_id")
    search.add_argument("query")
    search.add_argument("--mode", choices=[mode.value for mode in SearchMode], default="hybrid")
    return root


def main() -> None:
    args = parser().parse_args()
    settings = Settings()
    if args.command == "serve":
        uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
        return
    services = Services.create(settings)
    try:
        if args.command == "register":
            path = validate_repository_path(args.path, settings.resolved_roots())
            repository = register_repository(path)
            if not services.database.repository(repository.id):
                services.database.add_repository(repository)
            print(repository.model_dump_json(indent=2))
        elif args.command == "index":
            job = services.jobs.submit(args.repository_id, args.mode, None)
            print(services.jobs.wait(job.id).model_dump_json(indent=2))
        elif args.command == "search":
            response = services.retriever.search(
                args.repository_id, args.query, SearchMode(args.mode), 5, 20, 20
            )
            print(response.model_dump_json(indent=2))
    finally:
        services.close()


if __name__ == "__main__":
    main()
