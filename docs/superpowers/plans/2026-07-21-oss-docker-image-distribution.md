# OSS Docker Image Distribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Export the current publisher and downloader Docker images, upload immutable verified archives to `halecheng-oss`, and publish a public, integrity-checked `latest.json` pointer.

**Architecture:** Use a temporary local staging directory and the `ossutil 2.3.0` binary already present in the publisher container. Upload versioned objects before the mutable pointer, then verify every object through the public OSS endpoint. Bucket access is restricted to anonymous `GetObject` for the Docker image prefix.

**Tech Stack:** Docker Engine 29, gzip, SHA-256, JSON, Aliyun OSS, ossutil 2.3.0

---

### Task 1: Preflight and access boundary

**Files:**
- Read: `.env.publisher`
- Read: `publisher-secrets/oss_access_key_id`
- Read: `publisher-secrets/oss_access_key_secret`
- Read: `docs/superpowers/specs/2026-07-21-oss-docker-image-distribution-design.md`

- [ ] **Step 1: Verify source images and record immutable IDs**

Run:

```bash
docker image inspect traveler1314/pumper-source-publisher:publisher-v1 --format '{{.Id}}'
docker image inspect traveler1314/multi-ip-pumper:oss-v2 --format '{{.Id}}'
```

Expected: two `sha256:` IDs, matching the currently selected publisher and downloader images.

- [ ] **Step 2: Verify OSS endpoint and credential files without printing secrets**

Run:

```bash
test "$(sed -n 's/^OSS_BUCKET=//p' .env.publisher)" = halecheng-oss
test "$(sed -n 's/^OSS_REGION=//p' .env.publisher)" = cn-beijing
test -s publisher-secrets/oss_access_key_id
test -s publisher-secrets/oss_access_key_secret
```

Expected: exit status 0 with no secret output.

- [ ] **Step 3: Confirm the uploader is available**

Run:

```bash
docker exec pumper-source-publisher ossutil version
```

Expected: `ossutil version 2.3.0`.

### Task 2: Export immutable image archives

**Files:**
- Create temporarily: `/tmp/oss-docker-images-<random>/pumper-source-publisher.docker.tar.gz`
- Create temporarily: `/tmp/oss-docker-images-<random>/multi-ip-pumper.docker.tar.gz`
- Create temporarily: `/tmp/oss-docker-images-<random>/SHA256SUMS`
- Create temporarily: `/tmp/oss-docker-images-<random>/manifest.json`

- [ ] **Step 1: Create a private staging directory**

Run:

```bash
STAGING_DIR="$(mktemp -d /tmp/oss-docker-images.XXXXXXXX)"
chmod 700 "$STAGING_DIR"
```

Expected: an empty directory with mode `0700`.

- [ ] **Step 2: Export and compress the publisher image**

Run:

```bash
docker save traveler1314/pumper-source-publisher:publisher-v1 | gzip -1 > "$STAGING_DIR/pumper-source-publisher.docker.tar.gz"
gzip -t "$STAGING_DIR/pumper-source-publisher.docker.tar.gz"
```

Expected: exit status 0 and a non-empty valid gzip archive.

- [ ] **Step 3: Export and compress the downloader image**

Run:

```bash
docker save traveler1314/multi-ip-pumper:oss-v2 | gzip -1 > "$STAGING_DIR/multi-ip-pumper.docker.tar.gz"
gzip -t "$STAGING_DIR/multi-ip-pumper.docker.tar.gz"
```

Expected: exit status 0 and a non-empty valid gzip archive.

- [ ] **Step 4: Generate integrity metadata**

Run from the staging directory:

```bash
sha256sum pumper-source-publisher.docker.tar.gz multi-ip-pumper.docker.tar.gz > SHA256SUMS
```

Create `manifest.json` with schema version 1, UTC creation time, release ID, both full source image IDs, source tags, archive names, byte sizes, and SHA-256 values. Validate it with `python3 -m json.tool manifest.json`.

Expected: valid JSON and two SHA-256 lines.

### Task 3: Upload immutable release objects

**Files:**
- Read: temporary staging artifacts from Task 2
- Write externally: `oss://halecheng-oss/pumper/v1/docker-images/<release-id>/`

- [ ] **Step 1: Build a secret-safe ossutil invocation**

Copy `/usr/local/bin/ossutil` from the running publisher container into the staging directory. Create a temporary ossutil config under the staging directory with mode `0600`, populated from the existing secret files and the Beijing endpoint. Do not print the config or pass keys on the command line.

- [ ] **Step 2: Upload the two archives and metadata**

Run `ossutil cp --force` for each of:

```text
pumper-source-publisher.docker.tar.gz
multi-ip-pumper.docker.tar.gz
SHA256SUMS
manifest.json
```

Target: `oss://halecheng-oss/pumper/v1/docker-images/<release-id>/`.

Expected: all four commands exit 0.

- [ ] **Step 3: Confirm write access does not require broader Bucket operations**

Do not invoke bucket listing, deletion, ACL changes, or policy changes with the RAM credentials. If upload is denied, stop and report the denied object action instead of expanding permissions.

### Task 4: Configure and verify public read

**Files:**
- Write externally only if authorized: Bucket policy for `acs:oss:*:*:halecheng-oss/pumper/v1/docker-images/*`

- [ ] **Step 1: Test anonymous reads before policy changes**

Run an unauthenticated HTTPS `GET` for `manifest.json` and both archives' headers under the version URL.

Expected: HTTP 200. If the result is 403, the existing RAM key cannot be used to broaden policy; report the exact minimal anonymous `oss:GetObject` policy required in the Aliyun console and pause.

- [ ] **Step 2: Download and verify every immutable object**

Download `manifest.json`, `SHA256SUMS`, and both archives through `https://halecheng-oss.oss-cn-beijing.aliyuncs.com/pumper/v1/docker-images/<release-id>/`. Run:

```bash
sha256sum --check SHA256SUMS
gzip -t pumper-source-publisher.docker.tar.gz
gzip -t multi-ip-pumper.docker.tar.gz
```

Expected: both checksum lines report `OK`, and both gzip tests exit 0.

### Task 5: Publish pointer and validate deployment

**Files:**
- Create temporarily: `/tmp/oss-docker-images-<random>/latest.json`
- Write externally: `oss://halecheng-oss/pumper/v1/docker-images/latest.json`

- [ ] **Step 1: Publish latest pointer last**

Create `latest.json` containing schema version 1, release ID, manifest public URL, creation time, and both image entries. Upload it only after Task 4 passes.

Expected: upload exits 0.

- [ ] **Step 2: Verify latest pointer anonymously**

Run:

```bash
curl -fsS https://halecheng-oss.oss-cn-beijing.aliyuncs.com/pumper/v1/docker-images/latest.json | python3 -m json.tool
```

Expected: valid JSON pointing to the verified release.

- [ ] **Step 3: Verify archives load without changing running containers**

Run `docker image load --input` against the decompressed downloaded archives, then inspect the loaded tags and compare their IDs with Task 1.

Expected: both loaded image IDs exactly match the source IDs; running containers remain unchanged.

- [ ] **Step 4: Remove local temporary files**

After all checks complete, delete only the exact private staging and verification directories created by this plan. Preserve OSS objects and existing Docker images.

Expected: temporary directories no longer exist and the Git working tree has no new untracked archives.
