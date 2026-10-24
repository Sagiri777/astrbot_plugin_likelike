# astrbot_plugin_likelike

一个适用于 AstrBot `aiocqhttp` 适配器与 NapCat 的插件，用于为配置中的 QQ 号在每天的随机时间自动点赞。

## 功能

- 通过 AstrBot 的公开平台与客户端接口调用 NapCat `send_like`，不直连协议端 HTTP 服务
- 每天为每个配置的 QQ 号生成一个随机点赞时间
- 支持配置目标 QQ 列表、点赞数、发送模式和随机时间窗口
- 支持持久化保存当天计划和执行记录，并使用原子写入降低文件损坏风险
- 同一 QQ 的手动与定时任务串行执行，避免重复点赞
- `loop_single` 模式逐次记录成功次数；中途失败会保留计划并在重试时继续
- 提供 `/likelike status`、`/likelike run <qq号>`、`/likelike delete <qq号>` 指令

## 环境要求

- AstrBot，启用 `aiocqhttp` 消息平台
- NapCat，通过 OneBot v11 反向 WebSocket 连接 AstrBot
- 机器人账号能够访问目标用户资料；NapCat 返回频率过快、用户不存在等错误时，插件会记录日志并按计划重试

## 配置

在 AstrBot WebUI 中配置以下字段：

- `qq_list`：需要自动点赞的 QQ 号列表
- `like_times`：每次点赞的赞数，限制为 `1-10`
- `send_mode`：点赞发送模式
  - `single_request`：发送 1 次请求，`times=like_times`
  - `loop_single`：循环发送 `like_times` 次请求，每次 `times=1`
- `start_hour`：每日随机时间窗口开始小时
- `end_hour`：每日随机时间窗口结束小时

修改目标列表、点赞数、发送模式或时间窗口后，当天旧计划会失效并重新生成。

## 指令

- `/likelike status`：显示每个 QQ 的任务状态和插件记录的今日点赞数
- `/likelike run <qq号>`：立即为指定 QQ 点赞；完整成功后移除今天的计划任务，部分成功则保留任务
- `/likelike delete <qq号>`：移除指定 QQ 今天的计划任务
