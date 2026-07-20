# Publisher Docker Operations

## Purpose

`pumper-source-publisher` validates a controlled source list, signs the accepted manifest with Ed25519, uploads an immutable revision to Aliyun OSS, verifies it through the public endpoint, and updates `latest.json` last. It does not download workload traffic and does not expose a network service.

## OSS Setup

Create a private bucket in `cn-beijing`, enable versioning, and add anonymous `GetObject` permission only for:

```text
pumper/v1/latest.json
pumper/v1/releases/*
```

Do not grant anonymous bucket listing or writes. Configure lifecycle cleanup for noncurrent objects after 30 days.

Create a dedicated RAM user whose policy contains only `oss:GetObject` and `oss:PutObject` for the same prefix. Do not grant bucket ACL, bucket policy, delete, list, or account-management permissions.

## Secrets

The deployment expects three files:

```text
publisher-secrets/source_signing_private_key
publisher-secrets/oss_access_key_id
publisher-secrets/oss_access_key_secret
```

Set mode `0600` and keep the directory outside backups that are not encrypted:

```bash
chmod 600 publisher-secrets/*
```

The signing public key is not secret. Configure its base64 raw value on every multi-IP V2 downloader through `SOURCE_LIST_PUBLIC_KEY`.

Never pass AccessKeys or the signing private key as Docker build arguments, ordinary Compose environment values, command arguments, or GitHub Actions variables printed into logs.

## Deployment

Copy the non-secret example and set the bucket-specific values:

```bash
cp .env.publisher.example .env.publisher
bash install-publisher.sh
```

The Compose service uses a read-only root filesystem, drops all capabilities, sets `no-new-privileges`, mounts `/tmp` as tmpfs, persists `/state`, and publishes no ports.

Check status and logs:

```bash
docker compose -f docker-compose.publisher.yml --env-file .env.publisher ps
docker compose -f docker-compose.publisher.yml --env-file .env.publisher logs --tail 100 publisher
```

## Commands

Validate candidates without signing or OSS credentials:

```bash
docker compose -f docker-compose.publisher.yml --env-file .env.publisher run --rm publisher validate-only
```

Perform one locked publication transaction:

```bash
docker compose -f docker-compose.publisher.yml --env-file .env.publisher run --rm publisher publish-once
```

The default `scheduler` command runs daily at `03:17 Asia/Shanghai`. Failed attempts retry after 15 minutes, one hour, and then every six hours. A persistent exclusive lock prevents overlapping manual and scheduled publications.

## Safe Publication Order

1. Probe and accept at least three public IPv4 sources.
2. Build a revision greater than the previous successful revision.
3. Sign and locally verify the envelope.
4. Upload `pumper/v1/releases/<revision>.json`.
5. Download the immutable object from its public HTTPS URL and verify it.
6. Upload `pumper/v1/latest.json`.
7. Download and verify `latest.json`.
8. Atomically persist successful state under `/state`.

Any failure before step 6 leaves the prior `latest.json` untouched.

## Recovery

- OSS credential failure: revoke the RAM key, replace the two secret files, and restart the publisher.
- Incorrect source list: correct candidates and publish a higher revision. Downloaders reject decreasing revisions.
- Lost state: fetch and verify current `latest.json`, restore its revision to state, then resume.
- Signing-key compromise: stop publishing, revoke RAM credentials, rotate the key ID, deploy the new public key to downloaders, then resume with the new private key.
- Image regression: redeploy the previous recorded image digest rather than a mutable tag.

The first deployment uses `publisher-v1`. Promote the exact tested digest to `latest` only after three consecutive daily publications succeed.

