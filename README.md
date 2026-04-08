# astrbot_plugin_likelike

An AstrBot plugin that schedules daily QQ likes for configured users when the active platform adapter is `aiocqhttp`.

## Features

- Uses AstrBot's internal `aiocqhttp` adapter instead of calling the protocol endpoint over HTTP
- Generates one random like time per configured QQ number every day
- Supports configurable target QQ list, like count, and random time window
- Provides `/likelike status` and `/likelike run` commands for quick checks

## Configuration

Configure the plugin in AstrBot WebUI:

- `qq_list`: list of QQ numbers to like
- `like_times`: likes sent per target each time
- `start_hour`: start of the daily random window
- `end_hour`: end of the daily random window

## Commands

- `/likelike status`: show current plan and pending targets
- `/likelike run`: trigger one immediate round for all configured targets
