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

Create a dedicated directory on the public server and pull the publisher image:

```bash
mkdir -p /opt/pumper-publisher
cd /opt/pumper-publisher
docker pull traveler1314/pumper-source-publisher:publisher-v1
```

Generate the Ed25519 key pair once. The private key stays on the publisher; the
public key is copied to downloader configuration:

```bash
mkdir -p publisher-secrets publisher-config
chmod 700 publisher-secrets
docker run --rm \
  --user "$(id -u):$(id -g)" \
  -v "$PWD/publisher-secrets:/keys" \
  --entrypoint manifestctl \
  traveler1314/pumper-source-publisher:publisher-v1 \
  keygen \
  --private-key /keys/source_signing_private_key \
  --public-key /keys/source_signing_public_key
chmod 600 publisher-secrets/source_signing_private_key
```

Create the two RAM credential secret files without placing their values in the
Compose environment or shell history:

```bash
read -r -p 'OSS AccessKey ID: ' OSS_AK
read -r -s -p 'OSS AccessKey Secret: ' OSS_SK; printf '\n'
printf '%s' "$OSS_AK" > publisher-secrets/oss_access_key_id
printf '%s' "$OSS_SK" > publisher-secrets/oss_access_key_secret
unset OSS_AK OSS_SK
chmod 600 publisher-secrets/oss_access_key_id publisher-secrets/oss_access_key_secret
```

`install-publisher.sh` changes the three runtime secret files to UID/GID
`10001:10001` after pulling the image. This is required because local Docker
Compose secret mounts preserve host ownership while the publisher runs as UID
`10001`.

Download the deployment files, edit the non-secret bucket settings and candidate
URLs, then run the installer:

```bash
curl -fsSLO https://raw.githubusercontent.com/MengxingFusheng/steam-download-pumper/main/install-publisher.sh
chmod +x install-publisher.sh
bash install-publisher.sh
${EDITOR:-vi} .env.publisher
${EDITOR:-vi} publisher-config/candidates.json
bash install-publisher.sh
```

The first installer run creates the templates and stops until the three required
secret files and valid OSS values exist. Set
`OSS_PUBLIC_BASE_URL=https://<bucket>.oss-cn-beijing.aliyuncs.com/pumper/v1`.
Keep `publisher-config/candidates.json` free of query strings, credentials, and
private addresses.

The Compose service uses a read-only root filesystem, drops all capabilities, sets `no-new-privileges`, mounts `/tmp` as tmpfs, persists `/state`, and publishes no ports.

Check status and logs:

```bash
docker compose -f docker-compose.publisher.yml --env-file .env.publisher ps
docker compose -f docker-compose.publisher.yml --env-file .env.publisher logs --tail 100 source-publisher
```

After the first successful manual publication, verify both public objects before
enabling downloaders:

```bash
docker compose -f docker-compose.publisher.yml --env-file .env.publisher run --rm source-publisher publish-once
curl -fsS "https://<bucket>.oss-cn-beijing.aliyuncs.com/pumper/v1/latest.json" | head -c 200
cat publisher-secrets/source_signing_public_key
```

## Commands

Validate candidates without signing or OSS credentials:

```bash
docker compose -f docker-compose.publisher.yml --env-file .env.publisher run --rm source-publisher validate-only
```

Perform one locked publication transaction:

```bash
docker compose -f docker-compose.publisher.yml --env-file .env.publisher run --rm source-publisher publish-once
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
