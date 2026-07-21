# Multi-IP Pumper Fleet Operations Design

## Scope

Provide one-command deployment, status inspection, diagnosis, and rollback for
fewer than ten Tianjin servers running the same `multi-ip-pumper:oss-v2`
image. Hosts may use different SSH ports, SSH users, sudo passwords, container
IP lists, and bandwidth settings. All hosts use one dedicated SSH key, and sudo
requires a password.

This design manages only downloader hosts. It does not manage the OSS publisher,
Alibaba Cloud RAM, iKuai, DNS, or image builds.

## Approach

Use Ansible for remote execution and rolling deployment. Store ordinary host
settings in YAML inventory and encrypted credentials in a SOPS file protected
by an age key that exists only on the Codex workstation. The repository may
contain the encrypted SOPS document and its recipient configuration, but never
the age private key, plaintext passwords, SSH private key, Docker credentials,
or generated diagnostic reports.

The operator and Codex use one wrapper command rather than invoking playbooks
directly:

```bash
./ops/scripts/fleet deploy all
./ops/scripts/fleet deploy tj-node-03
./ops/scripts/fleet status
./ops/scripts/fleet diagnose all
./ops/scripts/fleet rollback tj-node-03
```

## Repository Layout

```text
ops/
├── ansible.cfg
├── requirements.yml
├── inventory/
│   ├── hosts.yml
│   ├── host_vars/
│   │   └── <host>.yml
│   └── secrets.sops.yml
├── playbooks/
│   ├── deploy.yml
│   ├── diagnose.yml
│   ├── rollback.yml
│   └── status.yml
├── roles/
│   └── multi_ip_pumper/
├── scripts/
│   └── fleet
└── README.md
```

`ops/reports/` is created at runtime and ignored by Git.

## Inventory Contract

`hosts.yml` defines stable aliases and group membership. Each
`host_vars/<host>.yml` contains non-secret settings:

- `ansible_host` and `ansible_port`;
- expected SSH host key fingerprint;
- `pumper_lan_ips`;
- target Mbps, base connections, and maximum connections per line;
- schedule window and remote OSS list settings;
- deployment directory and optional Docker registry/image override.

`secrets.sops.yml` maps each stable alias to `ansible_user` and
`ansible_become_password`. The SSH private key path is workstation-local and
provided through an ignored local environment file. The wrapper decrypts SOPS
to a protected temporary file in `/dev/shm` when available, passes it to
Ansible as an extra-vars file, and removes it on success, failure, interruption,
or timeout. It never passes passwords as command-line values.

## SSH Security

- Use one dedicated Ed25519 key with mode `0600`; do not reuse a personal key.
- Pin every server host key in a fleet-specific `known_hosts` file.
- Disable password SSH authentication in automation; passwords are used only
  for sudo through Ansible become.
- Enable host-key checking and never use `StrictHostKeyChecking=no`.
- Mark password-bearing Ansible tasks `no_log: true`.
- Refuse execution when SOPS, age, Ansible, the SSH key, or a pinned host key is
  missing.

## Deployment Transaction

The deploy playbook runs with `serial: 2` and `any_errors_fatal: true`:

1. Validate inventory and decrypt credentials locally.
2. Verify SSH, sudo, Docker, free space, architecture, time, and network access.
3. Read and record the current container image ID, image reference, config, and
   container state.
4. Pull the requested immutable image tag or digest.
5. Render deployment files into a staging directory and validate Compose.
6. Atomically save a rollback record and replace only `multi-ip-pumper`.
7. Wait for container health, then query `/api/status`, `/api/metrics`, and
   `/api/source-list` from a network path available on that host.
8. Require a non-empty LAN IP set, worker count within the configured cap,
   remote or last-known-good source origin, and a valid source-list revision.
9. Record a per-host result and continue to the next batch only after both hosts
   pass.

Deployment preserves the existing `/data` configuration unless an explicitly
versioned inventory setting changes it. It never edits iKuai or host routing.

## Failure And Rollback

If replacement fails after the old container is stopped, the role automatically
attempts one rollback on that host using the saved image ID and configuration.
It does not roll back already healthy hosts merely because a later batch fails.
The command exits non-zero and names failed, rolled-back, and untouched hosts.

Manual rollback restores the most recent successful rollback record for one
host. It refuses `all` unless the operator supplies an explicit confirmation
flag, preventing an accidental fleet-wide downgrade.

## Diagnosis

The diagnosis playbook is read-only and collects:

- OS, kernel, CPU, memory, disk, Docker, and container state;
- sanitized Compose configuration and effective non-secret environment;
- bounded recent container logs;
- interface, route, DNS, and IPv4 connectivity summaries;
- OSS `latest.json` reachability and downloader source-list status;
- current, 10-second, and 60-second throughput metrics;
- worker count, source health, restart count, and resource usage.

Reports are written under `ops/reports/<timestamp>/<host>/`. Collection removes
AccessKeys, passwords, authorization headers, cookies, query strings, private
keys, Docker auth, and SOPS values. Reports are ignored by Git and mode `0700`.

## Codex Workflow

After an image update, the operator can ask Codex to deploy the fleet. Codex
must first run `fleet status` and `fleet deploy ... --check`, summarize the
planned host/image/config changes, then run the actual deployment. Afterward it
runs fleet status and focused diagnosis for failed or underperforming hosts.

The wrapper supports machine-readable JSON output so Codex can report exact
results without scraping colored terminal output. Exit codes distinguish local
prerequisite failure, unreachable hosts, deployment failure, health-check
failure, and rollback failure.

## Testing And Acceptance

- Schema tests reject duplicate aliases/IPs, missing fingerprints, invalid
  ports, empty LAN IP sets, connection caps above 12, plaintext credential
  fields, and malformed OSS settings.
- Shell tests verify cleanup on success, failure, SIGINT, and SIGTERM and prove
  secrets never enter process arguments or reports.
- Ansible syntax and check-mode tests run in CI without real credentials.
- Integration tests use disposable SSH containers with different sudo passwords
  to cover successful deployment, one unreachable host, failed health checks,
  automatic rollback, and serial batch stopping.
- Acceptance requires a single command to deploy one host or all hosts, no
  plaintext secret in Git or logs, a healthy container on each successful host,
  and a useful per-host diagnostic report for every failure.

## Operational Assumptions

- The Codex Ubuntu workstation can reach every SSH endpoint.
- Servers run Linux with Docker Engine and Docker Compose V2, or permit their
  installation through sudo.
- Each server has a unique stable alias and pinned SSH host key.
- All downloader containers use the same image contract; host-specific runtime
  parameters are represented in inventory.
- Image publishing remains a separate, completed workflow. Fleet deployment
  consumes an already published immutable tag or digest.
