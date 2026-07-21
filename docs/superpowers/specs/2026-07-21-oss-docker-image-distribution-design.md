# OSS Docker 镜像分发设计

## 目标

将当前已验证的分发端和下载端 Docker 镜像导出到阿里云 OSS，供中国大陆服务器通过 HTTPS 下载并用 `docker load` 部署。OSS 仅作为镜像归档下载源，不模拟 OCI Registry。

## 上传对象

- 分发端：`traveler1314/pumper-source-publisher:publisher-v1`，当前镜像 ID `sha256:3b21f9baba17...`
- 下载端：`traveler1314/multi-ip-pumper:oss-v2`，当前镜像 ID `sha256:50465f01554f...`

执行前必须重新读取完整镜像 ID，避免使用过期的简称。每个归档使用 `docker save` 保留镜像标签，再使用 gzip 压缩。

## OSS 布局

目标 Bucket 为北京区域的 `halecheng-oss`。对象统一位于：

```text
pumper/v1/docker-images/<release-id>/
  pumper-source-publisher.docker.tar.gz
  multi-ip-pumper.docker.tar.gz
  SHA256SUMS
  manifest.json
pumper/v1/docker-images/latest.json
```

`release-id` 由两个镜像的短 ID 和 UTC 时间组成。版本目录不可变；`latest.json` 最后更新，用于指向已完整验证的版本。

## 数据流与校验

1. 检查 Docker daemon、两张目标镜像及 OSS 凭证文件可读。
2. 在项目外的临时目录导出并压缩镜像，避免污染 Git 工作树。
3. 计算归档 SHA-256 和字节大小，写入 `SHA256SUMS` 与 `manifest.json`。
4. 先上传两个归档和元数据到不可变版本目录。
5. 通过 OSS 公网 URL 下载对象并重新计算 SHA-256；任何对象不一致则停止，不更新 `latest.json`。
6. 所有版本对象验证通过后，最后上传 `latest.json`。
7. 验证 `latest.json` 和两个归档可匿名读取；临时本地归档验证完成后删除。

## 权限边界

- 仅对 `pumper/v1/docker-images/*` 授予匿名 `GetObject`。
- 不开放匿名 `PutObject`、`DeleteObject`、Bucket 列表、ACL 或策略管理。
- 使用已有 RAM 凭证上传，密钥只从 `publisher-secrets/` 读取，不写入命令参数、日志、归档或 Git。
- 如果现有 RAM 身份无权修改 Bucket Policy，则停止并给出需要在阿里云控制台添加的最小策略；不扩大现有密钥权限。

## 国内部署方式

部署机先下载 `latest.json`，再下载对应归档并校验 SHA-256：

```bash
gzip -dc pumper-source-publisher.docker.tar.gz | docker load
gzip -dc multi-ip-pumper.docker.tar.gz | docker load
```

加载后通过 `docker image inspect` 核对镜像 ID。现有 Compose 文件继续使用原标签，无需改镜像名称。

## 失败处理与回滚

- 导出、上传或公网回验任一步失败都不更新 `latest.json`。
- 已上传但未发布的版本目录可保留用于诊断，不影响当前版本。
- 回滚只需将 `latest.json` 恢复为上一个已验证版本，不覆盖历史归档。
- OSS 不具备原生 `docker pull` 协议；部署命令明确采用 HTTPS 下载加 `docker load`。

## 验收标准

- 两个归档均存在于同一不可变版本目录。
- OSS 公网下载结果与本地 SHA-256 完全一致。
- 匿名用户可读取镜像对象，但不能列 Bucket 或写入对象。
- 两个归档均能被 `docker load` 解析，加载后的镜像 ID 与源镜像一致。
- `latest.json` 只指向完成上述验证的版本。
