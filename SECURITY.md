# Security policy

## Supported version

Security fixes are applied to the latest v1.x release on `main`.

## Reporting a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/yangchen1234/repomesh/security/advisories/new). Do not open a public issue containing credentials, private repository content, network addresses, or exploit details. Include the affected version, impact, reproduction steps, and any proposed mitigation without attaching real secrets.

## Deployment boundary

RepoMesh is a trusted, single-user compute node. It is not a sandbox for hostile repositories and does not provide tenant isolation.

- Keep `REPOMESH_HOST=127.0.0.1` unless private remote access is intentional.
- Native remote binds must use an exact private-interface IP; wildcard binds are rejected.
- Set `REPOMESH_API_TOKEN` outside source control before remote binding.
- Prefer Tailscale or another encrypted private overlay and scope the host firewall to the intended client address.
- Never configure router port forwarding, Tailscale Funnel, or a public tunnel for port 8787.
- Limit `REPOMESH_REPOSITORY_ROOTS` to the smallest required directories.
- Treat Qdrant volumes, SQLite data, logs, model caches, and generated indexes as private runtime data.
- Rotate the node token if it is copied into Git, logs, screenshots, shell history, or an untrusted machine.

Health and Prometheus endpoints intentionally remain unauthenticated for private probes. Other `/v1` operations require the configured token. RepoMesh v1.0 does not terminate TLS itself.

## Secret handling

The repository ignores `.env`, token transfer files, databases, indexes, logs, model artifacts, caches, and bundles. Examples use only `<WINDOWS_TAILSCALE_IP>`, `<MAC_TAILSCALE_IP>`, `<REPOMESH_API_TOKEN>`, and `<REPOSITORY_PATH>` placeholders.
