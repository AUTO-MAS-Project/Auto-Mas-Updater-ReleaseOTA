"""使用下载站 Access Key 上传，回读公开地址确认已发布。"""

import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import httpx

BASE = "https://data.auto-mas.top"
UPLOAD_BASE = "https://data2.auto-mas.top"
MAX_BYTES = 32 * 1024 * 1024
UPLOAD_TIMEOUT = 300


FILE_KEYS = frozenset(
    f"maaend-{arch}-{channel}-delta"
    for arch in ("x64", "arm64")
    for channel in ("stable", "beta")
)
PUBLIC_PATHS = frozenset(
    f"/api/v1/files/maaend/ReleaseOTA/{key}/download" for key in FILE_KEYS
)


class PublishError(ValueError):
    pass


def target() -> tuple[str, str]:
    public_url = os.environ.get("AUTOMAS_PUBLIC_URL", "")
    token = os.environ.get("AUTOMAS_TOKEN", "")
    public = urlsplit(public_url)
    if (
        public.scheme != "https"
        or public.netloc != urlsplit(BASE).netloc
        or public.query
        or public.fragment
        or public.path not in PUBLIC_PATHS
    ):
        raise PublishError("请配置 AUTOMAS_PUBLIC_URL 为下载站的固定公开下载地址")
    if not token.strip() or "\r" in token or "\n" in token:
        raise PublishError("请配置 AUTOMAS_TOKEN Actions Secret")
    return public_url, token


async def matches(client: httpx.AsyncClient, url: str, expected: str) -> bool:
    try:
        digest, size = hashlib.sha256(), 0
        async with client.stream("GET", url, follow_redirects=True) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    return False
                digest.update(chunk)
        return digest.hexdigest() == expected
    except httpx.HTTPError:
        return False


def upload(
    file_id: int | None, token: str, content: bytes, *, filename: str
) -> dict | None:
    upload_path = (
        f"/api/v1/user/files/{file_id}/versions"
        if file_id is not None
        else "/api/v1/user/files"
    )
    if filename.removesuffix(".zip") not in FILE_KEYS or not filename.endswith(".zip"):
        raise PublishError("只允许 MaaEnd ReleaseOTA 的四个增量文件")
    # 下载站文档推荐境外 runner 直接使用 Cloudflare 入口，避开 EdgeOne 跳转。
    url = UPLOAD_BASE + upload_path
    with tempfile.TemporaryDirectory(prefix="copilot-publish-") as directory:
        source = Path(directory) / filename
        response = Path(directory) / "response.json"
        headers = Path(directory) / "headers.txt"
        cookies = Path(directory) / "cookies.txt"
        source.write_bytes(content)
        # 复用开服仓库的 curl multipart 传输；凭证仅通过 stdin 传入。
        # 只允许文档确认的数据中心主机及当前文件路径，避免任意跨站转发凭证。
        for _ in range(6):
            command = [
                "curl",
                "--silent",
                "--show-error",
                "--fail",
                "--proto",
                "=https",
                "--max-time",
                str(UPLOAD_TIMEOUT),
                "--max-filesize",
                str(MAX_BYTES),
                "--request",
                "POST",
                "--config",
                "-",
                "--form",
                f"file=@{source};type=application/zip",
                "--form-string",
                f"change_note=GitHub Actions run {os.environ.get('GITHUB_RUN_ID', 'manual')}",
                "--cookie",
                str(cookies),
                "--cookie-jar",
                str(cookies),
                "--dump-header",
                str(headers),
                "--output",
                str(response),
                "--write-out",
                "%{http_code} %{size_upload}",
                url,
            ]
            if file_id is None:
                command.extend(
                    [
                        "--form-string",
                        "project_key=maaend",
                        "--form-string",
                        "category_key=ReleaseOTA",
                        "--form-string",
                        "display_name=" + filename.removesuffix(".zip"),
                        "--form-string",
                        "description=MaaEnd 官方整包 SHA256 验证的多基准增量包",
                    ]
                )
            config = "header = " + json.dumps(f"Authorization: Bearer {token}") + "\n"
            try:
                result = subprocess.run(
                    command,
                    input=config,
                    text=True,
                    capture_output=True,
                    timeout=UPLOAD_TIMEOUT + 5,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise PublishError("下载站上传超时，未自动重试") from None
            metrics = result.stdout.split()
            status = int(metrics[0]) if metrics and metrics[0].isdecimal() else 0
            if status in {307, 308}:
                location = next(
                    (
                        line.partition(":")[2].strip()
                        for line in headers.read_text().splitlines()
                        if line.lower().startswith("location:")
                    ),
                    "",
                )
                if not location:
                    raise PublishError("下载站重定向缺少目标地址")
                location = urljoin(url, location)
                parsed = urlsplit(location)
                if (
                    parsed.scheme != "https"
                    or parsed.hostname
                    not in {urlsplit(BASE).hostname, urlsplit(UPLOAD_BASE).hostname}
                    or parsed.port not in {None, 443}
                    or parsed.path.rstrip("/") != upload_path
                    or parsed.username
                    or parsed.password
                ):
                    destination = f"{parsed.scheme}://{parsed.hostname}"
                    if parsed.port is not None:
                        destination += f":{parsed.port}"
                    raise PublishError(f"下载站上传重定向目标不符合要求：{destination}")
                url = location
                continue
            if status == 409:
                return None  # 仅随后公开回读一致才将重复内容视为成功。
            if result.returncode or not 200 <= status < 300:
                raise PublishError(
                    f"下载站上传失败（curl {result.returncode}，HTTP {status}，已发送 {metrics[1] if len(metrics) > 1 and metrics[1].isdecimal() else '未知'} 字节）"
                )
            if response.stat().st_size > MAX_BYTES:
                raise PublishError("上传响应过大")
            payload = json.loads(response.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict) or payload.get("code") != 0:
                raise PublishError("下载站未确认上传成功")
            data = payload.get("data")
            if not isinstance(data, dict) or (
                file_id is not None and data.get("id") != file_id
            ):
                raise PublishError("下载站返回的目标文件不一致")
            return data
    raise PublishError("下载站上传重定向次数过多")


async def publish(source: Path) -> None:
    public_url, token = target()
    filename = public_url.split("/")[-2] + ".zip"
    content = source.read_bytes()
    if not content or len(content) > MAX_BYTES:
        raise PublishError("增量包大小不合法")
    expected = hashlib.sha256(content).hexdigest()
    async with httpx.AsyncClient(timeout=40, follow_redirects=False) as client:
        if await matches(client, public_url, expected):
            print("公开下载内容一致，无须重复上传")
            return
        # 公开元数据定位 ID，不接受工作流任意指定数字 ID，防止更新到其他分类。
        response = await client.get(
            public_url.removesuffix("/download"), follow_redirects=True
        )
        if response.status_code == 404:
            file_id = None
        else:
            response.raise_for_status()
            existing = response.json().get("data")
            if not isinstance(existing, dict):
                raise PublishError("公开文件信息无效")
            actual = existing.get("file", existing)
            if (
                actual.get("project_key") != "maaend"
                or actual.get("category_key") != "ReleaseOTA"
                or actual.get("file_key") != filename.removesuffix(".zip")
                or not isinstance(actual.get("id"), int)
            ):
                raise PublishError("公开目标不属于 MaaEnd ReleaseOTA")
            file_id = actual["id"]
        data = await asyncio.to_thread(
            upload, file_id, token, content, filename=filename
        )
        if data is not None:
            keys = [
                data.get(key) for key in ("project_key", "category_key", "file_key")
            ]
            if any(not isinstance(key, str) or not key for key in keys):
                raise PublishError("上传结果缺少公开路径")
            actual_url = (
                BASE
                + "/api/v1/files/"
                + "/".join(quote(key, safe="") for key in keys)
                + "/download"
            )
            if actual_url != public_url:
                raise PublishError("公开下载地址与上传目标不一致")
            version = data.get("version")
            if (
                not isinstance(version, dict)
                or version.get("review_status") != "approved"
                or not isinstance(version.get("version_no"), int)
                or data.get("published_version_no") != version["version_no"]
            ):
                raise PublishError("增量包已上传但尚未发布，请使用有发布权限的账号凭证")
        for attempt in range(6):
            if await matches(client, public_url, expected):
                print(f"下载站公开内容校验通过：{public_url}")
                if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
                    with open(summary, "a", encoding="utf-8") as stream:
                        stream.write(f"\n下载站发布成功：[公开增量包]({public_url})\n")
                return
            if attempt < 5:
                await asyncio.sleep(5)
    raise PublishError("公开下载内容与增量包不一致，请检查审核状态或 CDN 缓存")
