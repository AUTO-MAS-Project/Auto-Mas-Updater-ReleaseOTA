# 下载站官方上传工具

`file_update.sh` 原样取自下载站公开工具接口：
https://data2.auto-mas.top/api/v1/tools/file_update.sh

2026-11-14 下载时，与 `/api/v1/tools` 返回的 SHA256 一致：
`efdea951f5ffde31e33218ad2f9c98311be0f100d7bbe5db3c64ebac39a251c3`。
发布器执行前再次核对该值；升级工具需要人工核对内容和摘要。

对应后端文档：AUTO-MAS-Project/AUTO-MAS-data-center-backend 的
`docs/API_EXAMPLES.md` 第 14.6 节。工具解包官方 Python TUS 客户端，创建独立虚拟环境，
使用完整哈希锁定的依赖。安装依赖时移除 `AUTOMAS_TOKEN`，上传时才传入访问密钥。
本仓库使用固定 HTTPS 入口、经公开分类信息确认的文件 ID、8 MiB 分片和最多 3 次续传重试。

TUS 只更新已有文件，所以首次用无可用增量计划的小 ZIP 创建文件记录。
最终成功以实际增量 ZIP 的公开 SHA256 回读为准。
