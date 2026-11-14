"""对照 GitHub 官方附件与已发布清单；未变化时跳过下载整包和制包。"""

import argparse
import io
import json
import os
import re
import zipfile

import httpx

from .maaend_delta import release_catalog, release_version, select_bases
from .publish import BASE, MAX_BYTES


class ReleaseNotReady(ValueError):
    """发行记录已经出现，但制包所需的官方附件尚未上传完成。"""


def expected_policy(releases: list[dict], channel: str, arch: str, tag: str = ""):
    candidates = [
        item
        for item in releases
        if bool(release_version(item["tag_name"])[1]) == (channel == "beta")
    ]
    if not candidates:
        return None
    target = (
        next(item for item in candidates if item["tag_name"] == tag)
        if tag
        else max(candidates, key=lambda item: release_version(item["tag_name"]))
    )
    tag = target["tag_name"]
    bases = select_bases(releases, tag, channel)

    def asset(release_tag):
        release = next(item for item in releases if item["tag_name"] == release_tag)
        package = next(
            (
                item
                for item in release["assets"]
                if item["name"] == f"MaaEnd-win-{arch}-{release_tag}.zip"
            ),
            None,
        )
        if (
            package is None
            or package.get("state") != "uploaded"
            or not 0 < package["size"] < 1024**3
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", package.get("digest") or "")
        ):
            raise ReleaseNotReady(f"{release_tag} 官方附件尚未就绪或缺少 SHA256")
        return package

    package = asset(tag)
    return {
        "version": 2,
        "mode": "range-zip",
        "channel": channel,
        "arch": arch,
        "target_version": tag,
        "target_size": package["size"],
        "target_sha256": package["digest"][7:],
        "bases": bases,
        "beta_base": bases[0] if channel == "beta" and bases else None,
    }, {asset(base)["digest"][7:] for base in bases}


def up_to_date(content: bytes, expected: dict, base_digests: set[str]) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            manifest = archive.getinfo("manifest.json")
            if manifest.file_size > 512 * 1024:
                return False
            policy = json.loads(archive.read(manifest))
        return (
            isinstance(policy, dict)
            and all(policy.get(key) == value for key, value in expected.items())
            and isinstance(policy.get("plans"), dict)
            and set(policy["plans"]) == base_digests
        )
    except (ValueError, KeyError, TypeError, zipfile.BadZipFile):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", choices=["stable", "beta"], required=True)
    parser.add_argument("--arch", choices=["x86_64", "aarch64"], required=True)
    parser.add_argument("--target-tag", default="")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    pending = ""
    try:
        expected = expected_policy(
            release_catalog(), args.channel, args.arch, args.target_tag
        )
    except ReleaseNotReady as error:
        if args.force:
            raise
        expected = None
        pending = str(error) + "，保留已发布文件，等待下次检查"
    needed, tag = False, ""
    if expected is not None:
        policy, base_digests = expected
        tag = policy["target_version"]
        architecture = "x64" if args.arch == "x86_64" else "arm64"
        url = f"{BASE}/api/v1/files/maaend/ReleaseOTA/maaend-{architecture}-{args.channel}-delta/download"
        needed = True
        if not args.force:
            with httpx.stream(
                "GET", url, follow_redirects=True, timeout=60
            ) as response:
                if response.status_code != 404:
                    response.raise_for_status()
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > MAX_BYTES:
                            raise ValueError("下载站增量包超过 32 MiB")
                    needed = not up_to_date(bytes(content), policy, base_digests)
    message = f"{args.arch}/{args.channel}: " + (
        pending or f"{tag or '无发行版'}，{'需要制包' if needed else '无变化，跳过'}"
    )
    print(message)
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"needed={str(needed).lower()}\ntag={tag}\n")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(f"\n{message}\n")


if __name__ == "__main__":
    main()
