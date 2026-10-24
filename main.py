from __future__ import annotations

import asyncio
import json
import random
import re
import shlex
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from aiocqhttp.exceptions import ActionFailed

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
    AiocqhttpAdapter,
)
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path


@dataclass(slots=True)
class LikePlanItem:
    user_id: str
    run_at: datetime
    retries: int = 0


@dataclass(slots=True)
class LikeResult:
    sent: int
    completed: bool


class LikeLikePlugin(Star):
    _PLAN_STORE_KEY = "daily_like_plan"
    _LIKE_LOG_STORE_KEY = "daily_like_log"
    _DAILY_LIKE_LIMIT = 10

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._data_dir = Path(get_astrbot_plugin_data_path()) / self.name
        self._plan_file = self._data_dir / "daily_like_plan.json"
        self._like_log_file = self._data_dir / "daily_like_log.json"
        self._stop_event = asyncio.Event()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._current_plan_day: date | None = None
        self._current_plan: list[LikePlanItem] = []
        self._completed_user_ids: set[str] = set()
        self._daily_like_day: date | None = None
        self._daily_like_counts: dict[str, int] = {}
        self._like_locks: dict[str, asyncio.Lock] = {}

    async def initialize(self) -> None:
        self._stop_event.clear()
        self._data_dir.mkdir(parents=True, exist_ok=True)
        await self._restore_plan()
        await self._restore_daily_like_log()
        await self._sync_task_state_with_logs()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        logger.info("[likelike] initialized")

    async def terminate(self) -> None:
        self._stop_event.set()
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        await self._persist_plan()
        await self._persist_daily_like_log()
        logger.info("[likelike] terminated")

    @filter.command("likelike")
    async def likelike_status(self, event: AstrMessageEvent, args_str: str = ""):
        raw_args = self._extract_command_args(event, args_str)
        tokens = self._parse_command_tokens(raw_args)
        subcommand = tokens[0].lower() if tokens else "status"
        if subcommand == "status":
            await self._ensure_today_like_log()
            await self._sync_task_state_with_logs()
            plan_day = (
                self._current_plan_day.isoformat() if self._current_plan_day else "N/A"
            )
            qq_list = ", ".join(self._get_target_user_ids()) or "empty"
            times = self._get_like_times()
            send_mode = self._get_send_mode()
            lines = [
                "LikeLike 状态",
                f"目标列表: {qq_list}",
                f"点赞数: {times}",
                f"发送模式: {send_mode}",
                f"计划日期: {plan_day}",
            ]
            for user_id in self._get_target_user_ids():
                today_like_count = self._get_today_like_count(user_id)
                schedule = next(
                    (item for item in self._current_plan if item.user_id == user_id),
                    None,
                )
                if user_id in self._completed_user_ids:
                    state = "已完成"
                elif schedule is not None:
                    state = f"待执行@{schedule.run_at.strftime('%H:%M:%S')}(重试{schedule.retries})"
                else:
                    state = "已移除"
                lines.append(
                    f"{user_id}（QQ） 今日已点赞={today_like_count} 状态={state}"
                )
            yield event.plain_result("\n".join(lines))
            return

        if subcommand == "run":
            if len(tokens) != 2 or not tokens[1].strip().isdigit():
                yield event.plain_result("用法：/likelike run <qq号>")
                return

            user_id = tokens[1].strip()
            if user_id not in self._get_target_user_ids():
                yield event.plain_result(f"QQ 号 {user_id} 不在 qq_list 配置中。")
                return

            result = await self._send_like(user_id)
            if result is None:
                yield event.plain_result(f"为 {user_id} 点赞失败，请稍后重试。")
                return

            today_like_count = self._get_today_like_count(user_id)
            if result.sent == 0:
                yield event.plain_result(
                    f"{user_id} 今天已经累计点赞 {today_like_count} 次，已移除今日任务。"
                )
                return

            removed = False
            if result.completed:
                removed = self._remove_planned_task(user_id)
                if removed:
                    await self._persist_plan()
            suffix = (
                " 已移除该 QQ 今天的计划任务。"
                if removed
                else " 本次仅部分成功，今日计划保留，可稍后重试。"
            )
            yield event.plain_result(
                f"已为 {user_id} 触发点赞 {result.sent} 次，"
                f"今日累计 {today_like_count} 次。" + suffix
            )
            return

        if subcommand == "delete":
            if len(tokens) != 2:
                yield event.plain_result("用法：/likelike delete <qq号>")
                return

            user_id = tokens[1].strip()
            if user_id not in self._get_target_user_ids():
                yield event.plain_result(f"QQ 号 {user_id} 不在 qq_list 配置中。")
                return

            removed = self._remove_planned_task(user_id)
            self._completed_user_ids.discard(user_id)
            if removed:
                await self._persist_plan()
                yield event.plain_result(f"已移除 {user_id} 今天的任务。")
                return
            yield event.plain_result(f"QQ 号 {user_id} 今天没有待执行任务。")
            return

        yield event.plain_result(
            "用法：/likelike status | /likelike run <qq号> | /likelike delete <qq号>"
        )

    async def _scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                now = datetime.now().astimezone()
                today = now.date()

                await self._ensure_today_like_log(today)

                if self._current_plan_day != today:
                    self._current_plan_day = today
                    self._completed_user_ids.clear()
                    self._current_plan = self._build_plan_for_day(today)
                    await self._sync_task_state_with_logs()
                    await self._persist_plan()
                    if self._current_plan:
                        readable_plan = ", ".join(
                            f"{item.user_id}@{item.run_at.strftime('%H:%M:%S')}"
                            for item in self._current_plan
                        )
                        logger.info("[likelike] today plan: %s", readable_plan)
                    else:
                        logger.info("[likelike] no valid targets configured for today")

                next_item = next(
                    (item for item in self._current_plan if item.run_at > now),
                    None,
                )
                due_items = [item for item in self._current_plan if item.run_at <= now]

                if due_items:
                    for item in due_items:
                        if item.user_id in self._completed_user_ids:
                            self._current_plan.remove(item)
                            await self._persist_plan()
                            continue
                        result = await self._send_like(item.user_id)
                        if result is not None and result.completed:
                            self._current_plan.remove(item)
                            await self._persist_plan()
                            continue
                        if item.retries >= 3:
                            logger.warning(
                                "[likelike] drop task for %s after %s retries",
                                item.user_id,
                                item.retries,
                            )
                            self._current_plan.remove(item)
                            await self._persist_plan()
                            continue
                        item.retries += 1
                        item.run_at = now + timedelta(minutes=10)
                        logger.info(
                            "[likelike] rescheduled %s to %s after retry %s",
                            item.user_id,
                            item.run_at.strftime("%H:%M:%S"),
                            item.retries,
                        )
                        await self._persist_plan()
                    continue

                if next_item is None:
                    next_day = datetime.combine(
                        today + timedelta(days=1),
                        time.min,
                        tzinfo=now.tzinfo,
                    )
                    await self._sleep_until(next_day)
                    continue

                await self._sleep_until(next_item.run_at)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("[likelike] scheduler loop failed: %s", exc)
                await self._sleep_seconds(60)

    def _build_plan_for_day(self, day: date) -> list[LikePlanItem]:
        user_ids = self._get_target_user_ids()
        if not user_ids:
            return []

        start_hour = self._get_int_config("start_hour", 8, min_value=0, max_value=23)
        end_hour = self._get_int_config("end_hour", 22, min_value=0, max_value=23)
        if end_hour <= start_hour:
            logger.warning(
                "[likelike] invalid time window %s-%s, fallback to 08-22",
                start_hour,
                end_hour,
            )
            start_hour = 8
            end_hour = 22

        tzinfo = datetime.now().astimezone().tzinfo
        start_dt = datetime.combine(day, time(hour=start_hour), tzinfo=tzinfo)
        end_dt = datetime.combine(day, time(hour=end_hour), tzinfo=tzinfo)
        span_seconds = int((end_dt - start_dt).total_seconds())
        if span_seconds <= 0:
            return []

        rng = random.Random(f"{day.isoformat()}:{','.join(user_ids)}")
        used_offsets: set[int] = set()
        plan: list[LikePlanItem] = []

        for user_id in user_ids:
            offset = rng.randrange(span_seconds)
            if span_seconds >= len(user_ids):
                while offset in used_offsets:
                    offset = rng.randrange(span_seconds)
                used_offsets.add(offset)
            plan.append(
                LikePlanItem(
                    user_id=user_id, run_at=start_dt + timedelta(seconds=offset)
                )
            )

        plan.sort(key=lambda item: item.run_at)
        return plan

    async def _restore_daily_like_log(self) -> None:
        stored = await self._load_persistent_like_log()
        if not isinstance(stored, dict):
            stored = await self.get_kv_data(self._LIKE_LOG_STORE_KEY, {})
        if not isinstance(stored, dict):
            self._daily_like_day = datetime.now().astimezone().date()
            self._daily_like_counts = {}
            return

        stored_day = stored.get("day")
        counts = stored.get("counts")
        today = datetime.now().astimezone().date().isoformat()
        if stored_day != today or not isinstance(counts, dict):
            self._daily_like_day = datetime.now().astimezone().date()
            self._daily_like_counts = {}
            await self.delete_kv_data(self._LIKE_LOG_STORE_KEY)
            await self._delete_persistent_like_log()
            return

        self._daily_like_day = datetime.now().astimezone().date()
        self._daily_like_counts = {
            str(user_id).strip(): max(0, min(self._DAILY_LIKE_LIMIT, int(count)))
            for user_id, count in counts.items()
            if str(user_id).strip().isdigit()
            and isinstance(count, (int, str))
            and str(count).strip().isdigit()
        }
        await self._persist_daily_like_log()

    async def _ensure_today_like_log(self, today: date | None = None) -> None:
        current_day = today or datetime.now().astimezone().date()
        if self._daily_like_day == current_day:
            return
        self._daily_like_day = current_day
        self._daily_like_counts = {}
        await self._persist_daily_like_log()

    async def _sync_task_state_with_logs(self) -> None:
        await self._ensure_today_like_log()
        changed = False
        for user_id in self._get_target_user_ids():
            if self._get_today_like_count(user_id) >= self._DAILY_LIKE_LIMIT:
                if user_id not in self._completed_user_ids:
                    self._completed_user_ids.add(user_id)
                    changed = True
                if self._remove_planned_task(user_id):
                    changed = True
        if changed:
            await self._persist_plan()

    async def _persist_daily_like_log(self) -> None:
        day = self._daily_like_day or datetime.now().astimezone().date()
        payload = {
            "day": day.isoformat(),
            "counts": {
                user_id: count
                for user_id, count in sorted(self._daily_like_counts.items())
                if count > 0
            },
        }
        await self.put_kv_data(self._LIKE_LOG_STORE_KEY, payload)
        await self._save_persistent_like_log(payload)

    async def _load_persistent_like_log(self) -> dict | None:
        if not self._like_log_file.exists():
            return None
        try:
            return await asyncio.to_thread(self._read_like_log_file)
        except Exception as exc:
            logger.warning("[likelike] failed to load daily like log: %s", exc)
            return None

    async def _save_persistent_like_log(self, payload: dict) -> None:
        try:
            await asyncio.to_thread(self._write_like_log_file, payload)
        except Exception as exc:
            logger.warning("[likelike] failed to save daily like log: %s", exc)

    async def _delete_persistent_like_log(self) -> None:
        try:
            await asyncio.to_thread(self._unlink_like_log_file)
        except Exception as exc:
            logger.warning("[likelike] failed to delete daily like log: %s", exc)

    def _read_like_log_file(self) -> dict | None:
        with self._like_log_file.open("r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else None

    def _write_like_log_file(self, payload: dict) -> None:
        self._write_json_atomic(self._like_log_file, payload)

    def _unlink_like_log_file(self) -> None:
        if self._like_log_file.exists():
            self._like_log_file.unlink()

    async def _restore_plan(self) -> None:
        stored = await self._load_persistent_plan()
        if not isinstance(stored, dict):
            stored = await self.get_kv_data(self._PLAN_STORE_KEY, {})
        if not isinstance(stored, dict):
            return

        stored_day = stored.get("plan_day")
        config_snapshot = stored.get("config")
        items = stored.get("items")
        today = datetime.now().astimezone().date().isoformat()

        if stored_day != today:
            await self._clear_stored_plan()
            return
        if config_snapshot != self._build_config_snapshot():
            await self._clear_stored_plan()
            return
        if not isinstance(items, list):
            await self._clear_stored_plan()
            return

        restored_items: list[LikePlanItem] = []
        completed = stored.get("completed_user_ids", [])
        self._completed_user_ids = {
            str(user_id).strip()
            for user_id in completed
            if str(user_id).strip().isdigit()
        }
        for item in items:
            if not isinstance(item, dict):
                continue
            user_id = str(item.get("user_id", "")).strip()
            run_at_raw = item.get("run_at")
            retries_raw = item.get("retries", 0)
            if not user_id or not user_id.isdigit() or not isinstance(run_at_raw, str):
                continue
            try:
                run_at = datetime.fromisoformat(run_at_raw)
                if run_at.tzinfo is None:
                    run_at = run_at.replace(tzinfo=datetime.now().astimezone().tzinfo)
                retries = int(retries_raw)
            except (TypeError, ValueError):
                continue
            restored_items.append(
                LikePlanItem(
                    user_id=user_id,
                    run_at=run_at,
                    retries=max(0, retries),
                )
            )

        self._current_plan_day = datetime.now().astimezone().date()
        self._current_plan = sorted(restored_items, key=lambda item: item.run_at)
        if restored_items:
            logger.info(
                "[likelike] restored %s pending task(s) from plugin storage",
                len(restored_items),
            )
        await self._persist_plan()

    async def _clear_stored_plan(self) -> None:
        self._current_plan_day = None
        self._current_plan = []
        self._completed_user_ids.clear()
        await self.delete_kv_data(self._PLAN_STORE_KEY)
        await self._delete_persistent_plan()

    async def _persist_plan(self) -> None:
        if self._current_plan_day is None:
            await self.delete_kv_data(self._PLAN_STORE_KEY)
            await self._delete_persistent_plan()
            return

        payload = {
            "plan_day": self._current_plan_day.isoformat(),
            "config": self._build_config_snapshot(),
            "completed_user_ids": sorted(self._completed_user_ids),
            "items": [
                {
                    "user_id": item.user_id,
                    "run_at": item.run_at.isoformat(),
                    "retries": item.retries,
                }
                for item in self._current_plan
            ],
        }
        await self.put_kv_data(self._PLAN_STORE_KEY, payload)
        await self._save_persistent_plan(payload)

    def _build_config_snapshot(self) -> dict[str, int | list[str] | str]:
        return {
            "qq_list": self._get_target_user_ids(),
            "like_times": self._get_like_times(),
            "send_mode": self._get_send_mode(),
            "start_hour": self._get_int_config(
                "start_hour", 8, min_value=0, max_value=23
            ),
            "end_hour": self._get_int_config("end_hour", 22, min_value=0, max_value=23),
        }

    async def _load_persistent_plan(self) -> dict | None:
        if not self._plan_file.exists():
            return None
        try:
            return await asyncio.to_thread(self._read_json_file)
        except Exception as exc:
            logger.warning("[likelike] failed to load persistent plan: %s", exc)
            return None

    async def _save_persistent_plan(self, payload: dict) -> None:
        try:
            await asyncio.to_thread(self._write_json_file, payload)
        except Exception as exc:
            logger.warning("[likelike] failed to save persistent plan: %s", exc)

    async def _delete_persistent_plan(self) -> None:
        try:
            await asyncio.to_thread(self._unlink_plan_file)
        except Exception as exc:
            logger.warning("[likelike] failed to delete persistent plan: %s", exc)

    def _read_json_file(self) -> dict | None:
        with self._plan_file.open("r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else None

    def _write_json_file(self, payload: dict) -> None:
        self._write_json_atomic(self._plan_file, payload)

    def _write_json_atomic(self, path: Path, payload: dict) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.flush()
        temp_path.replace(path)

    def _unlink_plan_file(self) -> None:
        if self._plan_file.exists():
            self._plan_file.unlink()

    async def _send_like(self, user_id: str) -> LikeResult | None:
        lock = self._like_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            return await self._send_like_locked(user_id)

    async def _send_like_locked(self, user_id: str) -> LikeResult | None:
        await self._ensure_today_like_log()

        if user_id in self._completed_user_ids:
            logger.info(
                "[likelike] skip %s because plugin record shows it is completed today",
                user_id,
            )
            return LikeResult(sent=0, completed=True)

        today_like_count = self._get_today_like_count(user_id)
        remaining_likes = self._DAILY_LIKE_LIMIT - today_like_count
        if remaining_likes <= 0:
            self._completed_user_ids.add(user_id)
            removed = self._remove_planned_task(user_id)
            if removed:
                await self._persist_plan()
            logger.info("[likelike] skip %s because today's like limit is reached", user_id)
            return LikeResult(sent=0, completed=True)

        client = self._get_aiocqhttp_client()
        if client is None:
            logger.warning("[likelike] aiocqhttp adapter is not available")
            return None

        like_times = min(self._get_like_times(), remaining_likes)
        send_mode = self._get_send_mode()
        sent_likes = 0

        try:
            if send_mode == "loop_single":
                for _ in range(like_times):
                    try:
                        await self._call_send_like(client, user_id, 1)
                    except Exception as exc:
                        logger.warning(
                            "[likelike] send_like failed for %s after %s successful like(s): %s",
                            user_id,
                            sent_likes,
                            exc,
                        )
                        break
                    sent_likes += 1
                    self._daily_like_counts[user_id] = today_like_count + sent_likes
                    await self._persist_daily_like_log()
            else:
                await self._call_send_like(client, user_id, like_times)
                sent_likes = like_times
                self._daily_like_counts[user_id] = today_like_count + sent_likes
                await self._persist_daily_like_log()
        except ActionFailed as exc:
            logger.warning("[likelike] send_like failed for %s: %s", user_id, exc)
            return None
        except Exception as exc:
            logger.exception(
                "[likelike] unexpected send_like error for %s: %s", user_id, exc
            )
            return None

        completed = sent_likes == like_times
        if completed:
            self._completed_user_ids.add(user_id)
        if sent_likes:
            logger.info(
                "[likelike] sent %s like(s) to %s, today total=%s",
                sent_likes,
                user_id,
                self._daily_like_counts[user_id],
            )
            return LikeResult(sent=sent_likes, completed=completed)
        return None

    async def _call_send_like(self, client: Any, user_id: str, times: int) -> None:
        payload = {"user_id": user_id, "times": times}
        api = getattr(client, "api", None)
        if api is not None and callable(getattr(api, "call_action", None)):
            await api.call_action("send_like", **payload)
            return
        call_action = getattr(client, "call_action", None)
        if callable(call_action):
            await call_action("send_like", **payload)
            return
        raise RuntimeError("当前 aiocqhttp 客户端不支持 call_action")

    def _remove_planned_task(self, user_id: str) -> bool:
        original_len = len(self._current_plan)
        self._current_plan = [
            item for item in self._current_plan if item.user_id != user_id
        ]
        return len(self._current_plan) != original_len

    def _get_today_like_count(self, user_id: str) -> int:
        try:
            count = int(self._daily_like_counts.get(user_id, 0))
        except (TypeError, ValueError):
            return 0
        return max(0, min(self._DAILY_LIKE_LIMIT, count))

    def _parse_command_tokens(self, args_str: str) -> list[str]:
        try:
            return shlex.split(args_str)
        except ValueError:
            return args_str.split()

    def _extract_command_args(self, event: AstrMessageEvent, fallback_args: str = "") -> str:
        full_message = (event.message_str or event.get_message_str() or "").strip()
        if full_message:
            normalized = re.sub(r"\s+", " ", full_message)
            command_match = re.match(r"^/?likelike(?:\s+(.*))?$", normalized, re.IGNORECASE)
            if command_match:
                return (command_match.group(1) or "").strip()
        return fallback_args.strip()

    def _get_aiocqhttp_client(self) -> Any | None:
        adapter = self._get_aiocqhttp_adapter()
        if adapter is None:
            return None
        get_client = getattr(adapter, "get_client", None)
        if callable(get_client):
            return get_client()
        return getattr(adapter, "bot", None)

    def _get_aiocqhttp_adapter(self) -> AiocqhttpAdapter | None:
        get_platform = getattr(self.context, "get_platform", None)
        if callable(get_platform):
            try:
                from astrbot.api.event.filter import PlatformAdapterType

                platform = get_platform(PlatformAdapterType.AIOCQHTTP)
                if isinstance(platform, AiocqhttpAdapter):
                    return platform
            except (AttributeError, ImportError, TypeError):
                pass
        platforms = self.context.platform_manager.get_insts()
        return next(
            (
                platform
                for platform in platforms
                if isinstance(platform, AiocqhttpAdapter)
            ),
            None,
        )

    def _get_target_user_ids(self) -> list[str]:
        raw_list = self.config.get("qq_list", [])
        if not isinstance(raw_list, list):
            return []

        user_ids: list[str] = []
        for item in raw_list:
            user_id = str(item).strip()
            if not user_id or not user_id.isdigit():
                continue
            if user_id not in user_ids:
                user_ids.append(user_id)
        return user_ids

    def _get_like_times(self) -> int:
        return self._get_int_config("like_times", 10, min_value=1, max_value=10)

    def _get_send_mode(self) -> str:
        send_mode = str(self.config.get("send_mode", "single_request")).strip()
        if send_mode not in {"single_request", "loop_single"}:
            return "single_request"
        return send_mode

    def _get_int_config(
        self, key: str, default: int, *, min_value: int, max_value: int
    ) -> int:
        raw_value = self.config.get(key, default)
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = default
        return max(min_value, min(max_value, value))

    async def _sleep_until(self, target_time: datetime) -> None:
        while not self._stop_event.is_set():
            now = datetime.now().astimezone()
            remaining = (target_time - now).total_seconds()
            if remaining <= 0:
                return
            await self._sleep_seconds(min(remaining, 300))

    async def _sleep_seconds(self, seconds: float) -> None:
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
