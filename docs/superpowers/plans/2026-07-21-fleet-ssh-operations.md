# Fleet SSH Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a secure one-command workflow that deploys, verifies, diagnoses, and rolls back the Multi-IP downloader across fewer than ten SSH hosts with per-host sudo passwords.

**Architecture:** A Python validator owns the inventory contract, while a Bash `fleet` wrapper owns prerequisites, SOPS decryption, cleanup, and Ansible dispatch. Ansible performs two-host rolling deployments using immutable images, saves per-host rollback records, and collects redacted diagnostics without managing iKuai or the OSS publisher.

**Tech Stack:** Ansible Core 2.19, Python 3.11+, PyYAML, SOPS, age, OpenSSH, Docker Engine/Compose V2, Bash, pytest.

---

## File Structure

- `ops/scripts/fleet`: stable CLI, SOPS lifecycle, and Ansible dispatch.
- `ops/lib/inventory.py`: inventory and decrypted-secret validation.
- `ops/inventory/`: public hosts, per-host settings, encrypted credentials, and pinned host keys.
- `ops/playbooks/`: deploy, status, diagnose, and rollback entry points.
- `ops/roles/multi_ip_pumper/`: deployment transaction and health contract.
- `ops/filter_plugins/redact.py`: recursive report sanitization.
- `ops/tests/integration/`: disposable SSH hosts with distinct sudo passwords.
- `tests/test_ops_*.py`: local contract and security tests.
- `ops/README.md`: setup and routine Codex workflow.

### Task 1: Tooling And Local-State Boundaries

**Files:**
- Modify: `.gitignore`
- Create: `requirements-ops.txt`
- Create: `ops/requirements.yml`
- Create: `ops/ansible.cfg`
- Create: `tests/test_ops_layout.py`

- [ ] **Step 1: Write the failing layout test**

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ops_runtime_state_is_ignored_and_ansible_is_hardened():
    ignored = (ROOT / ".gitignore").read_text()
    for value in ("ops/.env.local", "ops/.runtime/", "ops/reports/",
                  "ops/inventory/secrets.sops.yml", "ops/inventory/known_hosts"):
        assert value in ignored
    cfg = (ROOT / "ops/ansible.cfg").read_text()
    assert "host_key_checking = True" in cfg
    assert "retry_files_enabled = False" in cfg
    assert "display_args_to_stdout = False" in cfg
```

- [ ] **Step 2: Verify the test fails**

Run: `python3 -m pytest tests/test_ops_layout.py -q`

Expected: FAIL because the operations files do not exist.

- [ ] **Step 3: Add pinned dependencies and configuration**

`requirements-ops.txt` contains `ansible-core==2.19.1`, `PyYAML==6.0.2`, and
`pytest==8.4.1`. `ops/requirements.yml` pins `community.docker` to `4.7.0`.
`ops/ansible.cfg` enables host-key checking, disables retry files and argument
display, sets `forks = 2`, `timeout = 20`, and SSH options
`IdentitiesOnly=yes PasswordAuthentication=no`. Add all five ignored paths from
the test exactly as written.

- [ ] **Step 4: Verify and commit**

```bash
python3 -m pytest tests/test_ops_layout.py -q
git add .gitignore requirements-ops.txt ops/requirements.yml ops/ansible.cfg tests/test_ops_layout.py
git commit -m "chore: add fleet operations prerequisites"
```

Expected: PASS, followed by one focused commit.

### Task 2: Inventory Contract

**Files:**
- Create: `ops/inventory/hosts.yml`
- Create: `ops/inventory/host_vars/example-node.yml.example`
- Create: `ops/inventory/secrets.sops.yml.example`
- Create: `ops/lib/inventory.py`
- Create: `tests/test_ops_inventory.py`

- [ ] **Step 1: Write failing inventory tests**

Use a valid host with SSH address/port, `SHA256:` host fingerprint, LAN IPv4
list, target Mbps, base/max connections, schedule, health URL, OSS list URL, and
key ID. Add individual failing cases for duplicate aliases/endpoints, invalid
ports, missing fingerprints, empty or duplicate LAN IPs, non-IPv4 values,
maximum connections over 12, secret-looking keys in public YAML, missing secret
host entries, and extra secret host entries.

- [ ] **Step 2: Verify tests fail**

Run: `python3 -m pytest tests/test_ops_inventory.py -q`

Expected: FAIL because `ops.lib.inventory` does not exist.

- [ ] **Step 3: Implement the validator**

Implement `InventoryError(ValueError)`,
`load_yaml(path: Path) -> dict[str, object]`,
`validate_public_inventory(root: Path) -> dict[str, dict[str, object]]`,
`validate_secrets(hosts: set[str], secrets_path: Path) -> None`, and
`main(argv: Sequence[str] | None = None) -> int` as the complete public module
interface.

The CLI accepts `--inventory` and `--secrets`, emits compact JSON on success,
and one sanitized error on failure. Recursively reject public keys matching
`password|secret|token|access_key|private_key`. Require connections in `1..12`,
base connections no greater than max, unique IPv4 LAN addresses, HTTPS OSS and
health URLs, and exact host-set equality between public and secret data.

- [ ] **Step 4: Add safe examples**

`hosts.yml` contains an empty `downloaders` group. The host-vars example uses
documentation addresses and `pumper_deploy_dir: /opt/multi-ip-pumper`. The
secret example documents only this shape and is never accepted as runtime data:

```yaml
hosts:
  example-node:
    ansible_user: downloader-admin
    ansible_become_password: encrypted-by-sops
```

- [ ] **Step 5: Verify and commit**

```bash
python3 -m pytest tests/test_ops_inventory.py -q
git add ops/inventory ops/lib/inventory.py tests/test_ops_inventory.py
git commit -m "feat: validate fleet inventory"
```

Expected: PASS.

### Task 3: Encrypted Fleet CLI

**Files:**
- Create: `ops/scripts/fleet`
- Create: `tests/test_ops_fleet_cli.py`

- [ ] **Step 1: Write failing CLI tests using fake executables**

Test `status`, `deploy`, `diagnose`, and `rollback`; selector validation;
`deploy --check` forwarding; `/dev/shm` preference; cleanup after success,
failure, SIGINT, and SIGTERM; absence of passwords in argv/stdout; refusal of
`rollback all` without `--confirm-fleet-rollback`; and distinct exit codes for
prerequisite, inventory, unreachable, deployment, health, and rollback errors.

- [ ] **Step 2: Verify tests fail**

Run: `python3 -m pytest tests/test_ops_fleet_cli.py -q`

Expected: FAIL because the wrapper does not exist.

- [ ] **Step 3: Implement the wrapper**

Use `set -euo pipefail`, `umask 077`, and `EXIT HUP INT TERM` traps. Parse, but
never source, an ignored `ops/.env.local` containing only:

```text
FLEET_SSH_KEY=/absolute/path/to/fleet_ed25519
SOPS_AGE_KEY_FILE=/absolute/path/to/age-keys.txt
```

Verify `python3`, `ansible-playbook`, `sops`, `age`, and `ssh-keygen`; require
SSH key mode `0600`; decrypt SOPS into a random mode-`0600` file; validate it;
set strict host-key checking with the fleet known-hosts file; invoke exactly one
playbook with `--limit`, `--private-key`, and the temporary extra-vars file;
remove plaintext before preserving the Ansible exit status.

- [ ] **Step 4: Verify and commit**

```bash
python3 -m pytest tests/test_ops_fleet_cli.py -q
bash -n ops/scripts/fleet
git add ops/scripts/fleet tests/test_ops_fleet_cli.py
git commit -m "feat: add encrypted fleet command"
```

Expected: PASS.

### Task 4: Transactional Deployment

**Files:**
- Create: `ops/playbooks/deploy.yml`
- Create: `ops/roles/multi_ip_pumper/defaults/main.yml`
- Create: `ops/roles/multi_ip_pumper/tasks/preflight.yml`
- Create: `ops/roles/multi_ip_pumper/tasks/deploy.yml`
- Create: `ops/roles/multi_ip_pumper/tasks/verify.yml`
- Create: `ops/roles/multi_ip_pumper/tasks/main.yml`
- Create: `ops/templates/docker-compose.multi-ip.yml.j2`
- Create: `ops/templates/pumper.env.j2`
- Create: `tests/test_ops_deploy_playbook.py`

- [ ] **Step 1: Write failing static contract tests**

Assert `serial: 2`, `any_errors_fatal: true`, and `become: true`; maximum
connections never exceed 12; default image is
`traveler1314/multi-ip-pumper:oss-v2`; `/data` persists; and templates contain no
Docker socket, privileged mode, host networking, password, or OSS credential.

- [ ] **Step 2: Verify tests fail**

Run: `python3 -m pytest tests/test_ops_deploy_playbook.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement preflight and staging**

Validate Linux, Docker Engine, Compose V2, image architecture, at least 1 GiB
free space, HTTPS OSS reachability, and all inventory limits. Save releases at
`/opt/multi-ip-pumper/releases/<UTC timestamp>/`, current files under `current/`,
and rollback metadata at `rollback/latest.json`. Inspect the existing container
and image, render files mode `0600`, run `docker compose config --quiet`, pull
the immutable reference, atomically save rollback metadata, and recreate only
`multi-ip-pumper`.

- [ ] **Step 4: Implement verification and automatic rescue**

Wait at most 120 seconds for health and query `/api/status`, `/api/metrics`, and
`/api/source-list` through a required per-host health URL. Require non-empty LAN
IPs, workers no greater than LAN count times max connections, source origin
`remote` or `last-known-good`, and a positive revision. A rescue block restores
the previous image/config once and retains the failed release for diagnosis.

- [ ] **Step 5: Verify and commit**

```bash
python3 -m pytest tests/test_ops_deploy_playbook.py -q
ansible-playbook -i ops/inventory/hosts.yml ops/playbooks/deploy.yml --syntax-check
git add ops/playbooks/deploy.yml ops/roles ops/templates tests/test_ops_deploy_playbook.py
git commit -m "feat: add transactional fleet deployment"
```

Expected: PASS.

### Task 5: Status And Redacted Diagnosis

**Files:**
- Create: `ops/playbooks/status.yml`
- Create: `ops/playbooks/diagnose.yml`
- Create: `ops/roles/multi_ip_pumper/tasks/status.yml`
- Create: `ops/roles/multi_ip_pumper/tasks/diagnose.yml`
- Create: `ops/filter_plugins/redact.py`
- Create: `tests/test_ops_status_playbook.py`
- Create: `tests/test_ops_redact.py`
- Create: `tests/test_ops_diagnose_playbook.py`

- [ ] **Step 1: Write failing status and redaction tests**

Require one JSON result per host with reachability, image ID, state, restart
count, source origin/revision, current and 60-second Mbps, workers, and error
category. Feed redaction nested passwords, AccessKeys, bearer headers, cookies,
URL queries, private keys, Docker auth, and SOPS values; require `[REDACTED]`
while preserving metrics, paths, aliases, and HTTP status.

- [ ] **Step 2: Verify tests fail**

Run: `python3 -m pytest tests/test_ops_status_playbook.py tests/test_ops_redact.py tests/test_ops_diagnose_playbook.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement bounded collection**

Status converts API failures into structured errors and emits compact records
prefixed `FLEET_RESULT=`. Diagnosis collects bounded OS, CPU, memory, disk,
Docker inspect/stats, 300 log lines, interfaces, routes, DNS, OSS HEAD status,
and downloader API output. Never collect sensitive environment names. Save
redacted files beneath `ops/reports/<UTC timestamp>/<alias>/` with directory
mode `0700` and files mode `0600`.

- [ ] **Step 4: Verify and commit**

```bash
python3 -m pytest tests/test_ops_status_playbook.py tests/test_ops_redact.py tests/test_ops_diagnose_playbook.py -q
ansible-playbook -i ops/inventory/hosts.yml ops/playbooks/status.yml --syntax-check
ansible-playbook -i ops/inventory/hosts.yml ops/playbooks/diagnose.yml --syntax-check
git add ops/playbooks ops/roles/multi_ip_pumper/tasks/status.yml ops/roles/multi_ip_pumper/tasks/diagnose.yml ops/filter_plugins tests/test_ops_status_playbook.py tests/test_ops_redact.py tests/test_ops_diagnose_playbook.py
git commit -m "feat: add fleet status and diagnostics"
```

Expected: PASS.

### Task 6: Guarded Rollback

**Files:**
- Create: `ops/playbooks/rollback.yml`
- Create: `ops/roles/multi_ip_pumper/tasks/rollback.yml`
- Create: `tests/test_ops_rollback_playbook.py`

- [ ] **Step 1: Write failing rollback tests**

Require valid rollback JSON, reject paths outside the deployment directory,
restore the exact prior image/config, reuse deployment health checks, retain the
failed release, and emit a distinct rollback-failure category.

- [ ] **Step 2: Verify test fails**

Run: `python3 -m pytest tests/test_ops_rollback_playbook.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement rollback**

Read only the newest successful transaction, validate schema and canonical
paths, atomically restore files, recreate the service, include `verify.yml`, and
record `rolled_back_at`, previous image ID, and resulting image ID. Do not delete
failed deployment records.

- [ ] **Step 4: Verify and commit**

```bash
python3 -m pytest tests/test_ops_rollback_playbook.py -q
ansible-playbook -i ops/inventory/hosts.yml ops/playbooks/rollback.yml --syntax-check
git add ops/playbooks/rollback.yml ops/roles/multi_ip_pumper/tasks/rollback.yml tests/test_ops_rollback_playbook.py
git commit -m "feat: add guarded fleet rollback"
```

Expected: PASS.

### Task 7: Disposable SSH/Sudo Integration Fleet

**Files:**
- Create: `ops/tests/integration/Dockerfile.host`
- Create: `ops/tests/integration/compose.yml`
- Create: `ops/tests/integration/run.sh`
- Create: `ops/tests/integration/fixtures/`
- Create: `tests/test_ops_integration_assets.py`

- [ ] **Step 1: Write failing fixture tests**

Require three SSH hosts sharing one public key but using distinct sudo
passwords; generated host keys; a constrained fake Docker CLI; no privileged
containers or real Docker socket; and teardown of plaintext fixtures.

- [ ] **Step 2: Verify test fails**

Run: `python3 -m pytest tests/test_ops_integration_assets.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement disposable hosts and scenarios**

Use Debian SSH containers with `openssh-server`, `sudo`, and `python3`. The fake
Docker CLI serves deterministic `version`, Compose, inspect, stats, and logs
responses from fixture state. `run.sh` generates temporary SSH/age keys,
encrypts secrets, pins host keys, and tests: all-host success; unreachable host
stopping the next serial batch; unhealthy API causing rollback; diagnosis with
no password leak; and SIGTERM cleanup.

- [ ] **Step 4: Verify and commit**

```bash
python3 -m pytest tests/test_ops_integration_assets.py -q
bash ops/tests/integration/run.sh
git add ops/tests/integration tests/test_ops_integration_assets.py
git commit -m "test: add fleet SSH integration environment"
```

Expected: five scenarios print `PASS`, with no leftover fixture container or
plaintext file.

### Task 8: Operator Workflow And Controlled Rollout

**Files:**
- Create: `ops/README.md`
- Modify: `README.md`
- Modify: `ops/scripts/fleet`
- Create: `tests/test_ops_docs.py`

- [ ] **Step 1: Write failing documentation tests**

Require exact guidance for dependency installation, dedicated SSH/age key
creation, fingerprint enrollment, SOPS editing, host addition/removal, check
mode, deployment, status, diagnosis, rollback, credential rotation, and the rule
that real values never enter Git or chat.

- [ ] **Step 2: Verify test fails**

Run: `python3 -m pytest tests/test_ops_docs.py -q`

Expected: FAIL.

- [ ] **Step 3: Add guarded bootstrap commands and documentation**

Implement:

```bash
./ops/scripts/fleet init
./ops/scripts/fleet enroll-host <alias> <host> <port>
./ops/scripts/fleet edit-secrets
./ops/scripts/fleet validate
```

`init` refuses existing key paths. `enroll-host` fetches a candidate key with
`ssh-keyscan`, displays its SHA256 fingerprint, and requires explicit
confirmation before storing it. `edit-secrets` invokes SOPS directly. Document
the normal Codex sequence: `status`, `deploy --check`, deploy by immutable
digest, status, then diagnosis only for failed or underperforming hosts.

- [ ] **Step 4: Verify safe implementation**

```bash
python3 -m pytest -q
bash -n ops/scripts/fleet ops/tests/integration/run.sh
for play in ops/playbooks/*.yml; do ansible-playbook -i ops/inventory/hosts.yml "$play" --syntax-check; done
bash ops/tests/integration/run.sh
```

Expected: all tests, syntax checks, and five integration scenarios pass.

- [ ] **Step 5: Bootstrap real hosts without committing secrets**

For each server, collect alias, SSH endpoint, username, sudo password, LAN IPs,
bandwidth settings, and health URL interactively. Confirm its host fingerprint,
write public host vars, and edit credentials only through SOPS. Run:

```bash
./ops/scripts/fleet validate
./ops/scripts/fleet status all
./ops/scripts/fleet deploy all --check
```

Expected: all enrolled hosts are reachable, sudo works, and check mode lists
only intended changes.

- [ ] **Step 6: Canary and fleet rollout**

Deploy one host using the published immutable digest, verify it, then deploy the
remaining hosts in two-host batches:

```bash
./ops/scripts/fleet deploy tj-node-01 --image traveler1314/multi-ip-pumper@sha256:50465f01554f1b9e1b9b62caa512170b6b1de443d29cc71993bc3bf363d90525
./ops/scripts/fleet status tj-node-01
./ops/scripts/fleet deploy all --exclude tj-node-01
./ops/scripts/fleet status all
```

Expected: every successful host is healthy with the requested digest, valid OSS
revision, and bounded workers; failures return non-zero and produce redacted
reports.

- [ ] **Step 7: Commit only safe source and docs**

```bash
git add README.md ops/README.md ops/scripts/fleet tests/test_ops_docs.py
git commit -m "docs: add fleet operations workflow"
git status --short
```

Expected: ignored SOPS secrets, private keys, known-hosts, runtime files, and
reports do not appear in Git status.
