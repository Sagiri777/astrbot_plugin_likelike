# astrbot_plugin_likelike

一个适用于 `aiocqhttp` 适配器的 AstrBot 插件，用于为配置中的 QQ 号在每天的随机时间自动点赞。

## 功能

- 通过 AstrBot 框架内部的 `aiocqhttp` 适配器调用协议端接口，不使用 HTTP 直连
- 每天为每个配置的 QQ 号生成一个随机点赞时间
- 支持配置目标 QQ 列表、点赞数、发送模式和随机时间窗口
- 支持持久化保存当天计划和执行记录
- 提供 `/likelike status`、`/likelike run <qq号>`、`/likelike delete <qq号>` 指令

## 配置

在 AstrBot WebUI 中配置以下字段：

- `qq_list`：需要自动点赞的 QQ 号列表
- `like_times`：每次点赞的赞数，限制为 `1-10`
- `send_mode`：点赞发送模式
  - `single_request`：发送 1 次请求，`times=like_times`
  - `loop_single`：循环发送 `like_times` 次请求，每次 `times=1`
- `start_hour`：每日随机时间窗口开始小时
- `end_hour`：每日随机时间窗口结束小时

## 指令

- `/likelike status`：显示每个 QQ 的任务状态和插件记录的今日点赞数
- `/likelike run <qq号>`：立即为指定 QQ 点赞，成功后移除今天的计划任务
- `/likelike delete <qq号>`：移除指定 QQ 今天的计划任务
