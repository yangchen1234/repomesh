from __future__ import annotations

import argparse
import ipaddress

import uvicorn

from repomesh.config import Settings
from repomesh.models import SearchMode
from repomesh.repositories import register_repository, validate_repository_path
from repomesh.services import Services


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="repomesh")
    commands = root.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument(
        "--container-loopback-publish",
        action="store_true",
        help=(
            "allow an internal wildcard bind when the container port is published "
            "to host loopback only"
        ),
    )
    worker = commands.add_parser("worker", help="run an independent indexing worker")
    worker.add_argument("--worker-id")
    commands.add_parser("migrate", help="apply PostgreSQL coordination migrations")
    commands.add_parser("import-legacy-jobs", help="import historical SQLite jobs after stopping the old API")
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


def validate_serve_bind(settings: Settings, container_loopback_publish: bool) -> None:
    """Reject ambiguous remote exposure before Uvicorn opens a socket."""
    try:
        address = ipaddress.ip_address(settings.host.strip("[]"))
    except ValueError as exc:
        raise SystemExit(
            "REPOMESH_HOST must be an exact loopback, Tailscale, or LAN IP address"
        ) from exc

    if address.is_loopback:
        return
    if address.is_unspecified:
        if container_loopback_publish:
            return
        raise SystemExit(
            "refusing wildcard REPOMESH_HOST; bind to an exact Tailscale or LAN IP"
        )
    if not settings.api_token or not settings.api_token.strip():
        raise SystemExit(
            "REPOMESH_API_TOKEN is required when REPOMESH_HOST is not loopback"
        )


def main() -> None:
    args = parser().parse_args()
    settings = Settings()
    if args.command == "serve":
        from repomesh.api import create_app

        validate_serve_bind(settings, args.container_loopback_publish)
        uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
        return
    services = Services.create(settings)
    try:
        if args.command == "worker":
            from repomesh.services import configure_logging
            from repomesh.worker import Worker

            configure_logging()
            Worker(services, args.worker_id).run()
        elif args.command == "migrate":
            print("PostgreSQL coordination migrations applied")
        elif args.command == "import-legacy-jobs":
            count = services.coordination.import_legacy_jobs(services.database.fetchall("SELECT * FROM jobs"))
            print(f"Imported {count} legacy jobs")
        elif args.command == "register":
            path = validate_repository_path(args.path, settings.resolved_roots())
            repository = register_repository(path)
            if not services.database.repository(repository.id):
                services.database.add_repository(repository)
            print(repository.model_dump_json(indent=2))
        elif args.command == "index":
            if not services.database.repository(args.repository_id):
                raise SystemExit("repository not found")
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
