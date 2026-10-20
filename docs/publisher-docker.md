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
Query parameters required by download endpoints are supported. Keep
`publisher-config/candidates.json` free of credentials, fragments, and private
addresses; every connection and redirect is resolved and pinned to public IPv4.

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

Perform one locked publication transaction. The scheduler owns the publication
lock for its lifetime, so stop it before starting the one-shot publisher and
restart it afterwards, including when publication fails:

```bash
docker compose -f docker-compose.publisher.yml --env-file .env.publisher stop source-publisher
docker compose -f docker-compose.publisher.yml --env-file .env.publisher run --rm source-publisher publish-once
docker compose -f docker-compose.publisher.yml --env-file .env.publisher up -d source-publisher
```

`publish-once` always rechecks sources and creates a higher revision, including on
the same day. The default `scheduler` command runs daily at `03:17 Asia/Shanghai`. Failed attempts retry after 15 minutes, one hour, and then every six hours. A persistent exclusive lock prevents overlapping manual and scheduled publications.

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

## Discovery and throughput selection

The default candidate template now includes the 22 download endpoints verified
against the [official LibreSpeed directory](https://librespeed.org/backend-servers/servers.php)
on 2026-10-19, with `ckSize=64` for the official PHP backend (its default
response is only 4 MiB), alongside the existing speed-test endpoints and Ubuntu mirrors.
These initial entries allow probing to continue if a directory is temporarily
unavailable. Each daily run also discovers current paths from the LibreSpeed JSON
directory and the Ookla XML directory, plus the explicitly configured ISCAS and
Huawei Ubuntu release directories. A directory HTTP 403 is a normal failure: it is
reported and the last successfully parsed directory cache is retained for at most
seven days. No access-control bypass is attempted.

Candidate merging prefers manual entries, then the current verified signed
manifest, then discovery, with deduplication and a 200 URL limit. Setting an
existing manual entry to `enabled: false` excludes that exact URL from all inputs.
`DISCOVERY_ENABLED=false` disables directory requests; the signed manifest and
manual candidates still participate.

Add controlled mirror directories to the existing candidate file:

```json
{
  "schema": 1,
  "sources": [],
  "directories": [
    {
      "kind": "mirror",
      "url": "https://mirror.iscas.ac.cn/ubuntu-releases/24.04/",
      "enabled": true,
      "pattern": "ubuntu-[0-9.]+-live-server-amd64\\.iso",
      "purpose": "Existing public Ubuntu release mirror used by this deployment"
    }
  ]
}
```

Mirror discovery reads only that listing, matches filenames, and rejects links or
redirects leaving the configured host/path. It never recursively crawls a site.
A configured `librespeed` or `ookla` entry with the same URL and `enabled: false`
disables the corresponding built-in directory. All directory reads have a 2 MiB
body limit, 10 second deadline and three-redirect limit. DNS and HTTP activity
share the publication cancellation token and 30 minute transaction deadline.

The two-stage probe uses these defaults:

| Stage | Transfer limit | Time limit | Concurrency |
| --- | --- | --- | --- |
| Every candidate initial screen | 2 MiB per request | 5 seconds | 4 |
| Up to 40 screened endpoints, two sustained rounds each | Continuous streaming; bodies discarded | 15 seconds per round, first 3 seconds warmup | 1 endpoint, 12 connections |

All endpoint types use the same node Go engine for sustained measurements.
Both rounds must succeed; the lower throughput must reach `MIN_PROBE_MBPS`
(default 100). Endpoint protocols and paths compete on measured quality before
host quotas are applied. Each host's best endpoint is considered first, followed
by secondary endpoints, with at most two URLs per host and the configured total
(up to 100). The discovery target remains 12 URLs across eight hosts; less than
three hosts is explicitly reported as degraded redundancy. Publication aborts
without replacing `latest.json` when no qualifying source exists.

`MIN_HEALTHY_SOURCES` accepts 1–30 and retains the historical default of three;
the current example sets one. Upgrade node readers and set
`SOURCE_LIST_MIN_SOURCES=1` **before** publishing fewer than three URLs. The
signed schema stays at version 1 and `probe_mbps` records the sustained score.
Daily scheduling, retries and the 30-minute whole-publication deadline remain
in force; only fully measured candidates can be published. Initial screening
still uses `PROBE_CONCURRENCY` and `PROBE_TIMEOUT_SECONDS` applies to legacy
Python probe callers, while production sustained rounds are bounded separately.

Inspect `/state/last-probes.json` for directory failures, discovery counts,
per-source speed and success rate, rejected/slow counts, total measured transfer,
elapsed time and quantity target status. Successful `/state/state.json` also
contains the summary. Scheduler logs include the summary after both successful
and failed attempts. The manifest remains schema 1 with unchanged signature,
72-hour expiry and immutable-release publication order.

If the host uses DNS that returns fake/private addresses for a public directory,
the publisher rejects those addresses. Set `PUBLISHER_DOH_URL=https://dns.alidns.com/resolve` to use an explicit trusted
HTTPS JSON DNS resolver for candidates and directory requests, or correct the
deployment DNS. The DoH provider itself must resolve to public IPv4 through system
DNS. Its connection is also pinned with HTTPS hostname verification. Responses
are limited to 64 KiB and five seconds, redirects are rejected, the question and
CNAME/A chain are checked, and successful answers are cached for at most 300
seconds. Candidate and redirect answers must still be entirely public IPv4. The
host DNS configuration is unchanged.

### Ookla 直连下载源（2026-10-20）

发布服务器通过 Ookla 网页服务器接口发现候选，`OOKLA_DISCOVERY_ENABLED=true` 开启，`OOKLA_SEARCH_TERMS=Tianjin,Beijing,Shanghai` 配置地区检索。接口没有返回某地区时，也会从已有人工或上一版清单中的 Ookla 静态图片地址提取同主机的现代下载端点，逐个实测验证；节点只消费签名源清单，不自行发现。

下载模板包含 `size=67108864&guid={{guid}}&nocache={{nonce}}`，每次请求展开新标识。初筛拒绝空响应和错误页，之后所有端点统一执行持续多连接复测并达到 `MIN_PROBE_MBPS`，不再按现代 Ookla 类型豁免门槛或预留名额。每主机先取最快端点，最多两个 URL；`MAX_HEALTHY_SOURCES` 支持最多 100。

Go 下载引擎支持绑定出口的 HTTP/2 协商，继续支持 HTTP/1.1，并拒绝空响应和 HTML/JSON 错误页。每条线路连接上限仍为 12；有进度的低速线路继续运行，由现有健康机制换源和探测恢复。


### 当前生产发布器：192.168.1.70

2026-10-20 起，生产发布器迁移至 `192.168.1.70:60001`，项目目录 `/opt/source-publisher`，Compose 项目名 `source-publisher`。采用 `network_mode=host`，默认路由源地址 `192.168.1.70`、网关 `192.168.1.1`。使用该主机的 DNS 和出口进行完整候选发现、实际数据验证及签名发布；未迁移 Ubuntu 的发现缓存、测速结果或静态 DNS 覆盖。

Ubuntu 上的旧容器、发布器专用数据卷、镜像及本机运行配置和密钥目录已删除，不再承担生产筛选。签名密钥沿用，下载节点无需替换信任公钥。已有清单只作为候选地址来源，必须在 `.70` 重新验证后才能进入新清单。

```bash
cd /opt/source-publisher
docker compose -p source-publisher -f compose.yml up -d --no-deps --no-build --pull never source-publisher
```

首次发布由 `/opt/source-publisher/release/initial-publish.sh` 执行，结果位于同目录 `initial-publish.json` / `initial-publish.exit`，成功后自动启动每日调度服务。日常手动重新发布时先停止每日服务，使用同一 Compose 的 `run --rm --no-deps source-publisher publish-once` 完成独占发布后再启动每日服务，避免同一签名清单存在多个写入者。
