# Auto-Mas-Updater-ReleaseOTA

MaaEnd 发行包增量的独立生成、验证、发布仓库。安装与更新选择仍由 AUTO-MAS 主程序负责。

## MaaEnd 增量包（GitHub Range 的备用源）

MAS 优先直接从 GitHub 官方完整 ZIP 按 HTTP Range 重建。GitHub 路径不限起始版本，以 40% 读取量、3000 变化条目及网络条件决定是否回退。
GitHub 不支持区间、超时、
变化过大或重建失败时，再尝试本工作流发布的自建站字节增量；均不可用则全量。
两条路径最后都必须通过从 GitHub 独立取得的官方整包大小和 SHA256，不运行 MaaEnd。

工作流 **生成 MaaEnd 增量包并上传下载站** 在本仓库 `main` 手动运行。输入目标官方标签
（留空选该通道最新版本）、架构 `x64` / `arm64` 和通道 `stable` / `beta`。
不再手选起始版本，发布器会从官方完整分页发行记录自动确定备用包范围（不限制 GitHub Range）：

- 正式目标只包含之前最近三个正式版，例如 `3.0.0 / 2.9.0 / 2.8.0 → 3.1.0`，更早版本全量。
- beta 必须有同号正式基准，例如 `3.1.0 → 3.1.0-beta.8`；客户端允许该正式版或同号更早 beta。
  `3.0.0` 不能增量升到它。`3.2.0-beta.8` 缺少 `3.2.0` 正式版时，发布空计划，客户端全量。
- beta 备用包以正式版为固定基准，客户端额外保留一份经过官方校验的正式 ZIP。
  在 b4 上用该基准重建完整 b8 ZIP，不往现有目录叠补丁。没有可信基准缓存时全量，
  不会为了获取基准而暗中下载一个大包。GitHub Range 则可以复用当前 beta 的缓存 ZIP。

发布器下载目标和最多三个基准官方 ZIP，分别验证官方 SHA256；各基准的旧数据区间独立，
目标新增字节池合并去重。发布前从每个基准重建并验证目标整包。
删除、改名、重复文件和被回退的内容通过重建目标完整快照处理，安装沿用 MAS 现有事务与用户文件保护。
协议 v2，旧客户端不识别时安全回退全量；v1 工具保留兼容测试，新客户端拒绝无版本范围的旧发布包。

产物先保留为 7 天的 Actions artifact，再用 `publish.py` 上传及公开回读校验。
任一计划不能节省流量、合并包超过 32 MiB、清单超过 512 KiB、生成/验证失败或凭据缺失，
工作流报错，不替换已发布文件。旧发布包与当前目标 SHA256 不匹配时客户端全量。
可重复同样输入重试；首次生成完成前空计划会让客户端安全回退全量。

在下载站 MaaEnd → ReleaseOTA 创建四个独立文件，键为 `maaend-{arch}-{channel}-delta`。
例如 x64 正式通道地址为
`https://data.auto-mas.top/api/v1/files/maaend/ReleaseOTA/maaend-x64-stable-delta/download`。
不要复用作业或养成快照的文件 ID 和密钥。

只需在本仓库 Actions Secret 配置 `AUTOMAS_MAAEND_DELTA_TOKEN`。
发布器固定只接受 `maaend / ReleaseOTA` 下上述四个文件键，首次上传自动创建目标文件，
后续通过公开元数据确认项目、分类、文件键和 ID 后上传新版本，不接受任意文件 ID 覆写。
密钥须限制为 MaaEnd 项目的 ReleaseOTA 分类；不会读取、创建或上传到其他分类。
首次创建使用小于 32 KiB 的空计划 ZIP，通过 curl multipart 建立文件记录；这段时间客户端全量。
实际增量包和后续版本复用下载站官方 `file_update.sh`，通过 TUS 每片 8 MiB 上传，
失败按服务器偏移续传，避免整包单次请求触发 Cloudflare 524。服务端验证 SHA256，
发布器还必须从公开下载地址重新读取整个增量包并验证 SHA256，才报告成功。
官方上传工具固定版本随仓库保存，并校验其 SHA256；凭证通过环境传递，不写入参数或源码。

主程序协议实现：[AUTO-MAS #1985](https://github.com/AUTO-MAS-Project/AUTO-MAS/pull/1985)（已合并）；
下载地址迁移：[AUTO-MAS #1992](https://github.com/AUTO-MAS-Project/AUTO-MAS/pull/1992)。
完整客户端说明：[MaaEnd 双路径增量更新](https://github.com/youngslin/AUTO-MAS/blob/fix/maaend-releaseota-repository/docs/maaend-incremental-update.md)。

## 迁移验收

- 本地生成、三基准合并与上传边界共 8 项测试通过。
- [首次真实制包与上传](https://github.com/AUTO-MAS-Project/Auto-Mas-Updater-ReleaseOTA/actions/runs/43601819918)
  完成官方整包校验和增量重建验证，x64 beta 产物为 15,277,016 字节，Actions artifact 已保留。
- [延长上传时限后的复核](https://github.com/AUTO-MAS-Project/Auto-Mas-Updater-ReleaseOTA/actions/runs/43602071959)
  显示文件已完整发送，但下载站源站返回 HTTP 524；本机对同一包的独立上传也复现该错误。
  尚未确认公开发布，不能把制包成功当成站点上线。
- 原先整包 multipart 的超时不能靠延长客户端等待解决；现改用站点官方 TUS 工具。
  首次创建失败不自动重复 POST，分片允许受控重试；重跑先做公开摘要去重。
