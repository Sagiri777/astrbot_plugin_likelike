# Changelog

## v0.3.0

- 适配新版 AstrBot 的公开平台与客户端接口，优先使用 `get_platform()`、`get_client()` 和 `client.api.call_action()`，并保留旧版回退。
- 按当前 NapCat `send_like` 接口传递字符串 QQ 号和整数点赞次数。
- 为同一 QQ 的定时与手动点赞增加异步锁，避免并发重复执行。
- 修复 `loop_single` 模式部分成功后未记录已点赞次数、重试可能超额的问题。
- 部分成功时保留当日计划，完整成功后才移除任务。
- 兼容恢复不带时区的旧计划时间，并正确保留空任务但已完成的当日状态。
- 计划与点赞记录改为临时文件替换写入，降低异常退出导致 JSON 损坏的概率。
- 校验 `/likelike run` 的 QQ 号参数。

## v0.2.0

- 修复 `/likelike run <qq号>` 与 `/likelike delete <qq号>` 在 AstrBot 命令参数绑定下无法正确读取完整参数的问题。
- 新增插件更新日志文件，避免后台提示缺少 `CHANGELOG.md`。
