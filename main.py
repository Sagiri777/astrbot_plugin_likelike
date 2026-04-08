from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

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


class LikeLikePlugin(Star):
    _PLAN_STORE_KEY = "daily_like_plan"

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._data_dir = Path(get_astrbot_plugin_data_path()) / self.name
        self._plan_file = self._data_dir / "daily_like_plan.json"
        self._stop_event = asyncio.Event()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._current_plan_day: date | None = None
        self._current_plan: list[LikePlanItem] = []

    async def initialize(self) -> None:
        self._stop_event.clear()
        self._data_dir.mkdir(parents=True, exist_ok=True)
        await self._restore_plan()
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
        logger.info("[likelike] terminated")

    @filter.command("likelike")
    async def likelike_status(self, event: AstrMessageEvent, args_str: str = ""):
        subcommand = args_str.strip().lower()
        if not subcommand or subcommand == "status":
            plan_day = (
                self._current_plan_day.isoformat() if self._current_plan_day else "N/A"
            )
            pending = [
                f"{item.user_id}@{item.run_at.strftime('%H:%M:%S')}(r{item.retries})"
                for item in self._current_plan
                if item.run_at > datetime.now().astimezone()
            ]
            qq_list = ", ".join(self._get_target_user_ids()) or "empty"
            times = self._get_like_times()
            yield event.plain_result(
                "LikeLike status\n"
                f"targets: {qq_list}\n"
                f"times: {times}\n"
                f"plan_day: {plan_day}\n"
                f"pending: {', '.join(pending) if pending else 'none'}"
            )
            return

        if subcommand == "run":
            count = 0
            for user_id in self._get_target_user_ids():
                if await self._send_like(user_id):
                    count += 1
            yield event.plain_result(f"Triggered likes for {count} target(s).")
            return

        yield event.plain_result("Usage: /likelike status | /likelike run")

    async def _scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                now = datetime.now().astimezone()
                today = now.date()

                if self._current_plan_day != today:
                    self._current_plan_day = today
                    self._current_plan = self._build_plan_for_day(today)
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
                        if await self._send_like(item.user_id):
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
            await self.delete_kv_data(self._PLAN_STORE_KEY)
            return
        if config_snapshot != self._build_config_snapshot():
            await self.delete_kv_data(self._PLAN_STORE_KEY)
            return
        if not isinstance(items, list):
            await self.delete_kv_data(self._PLAN_STORE_KEY)
            return

        restored_items: list[LikePlanItem] = []
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

        if not restored_items:
            await self.delete_kv_data(self._PLAN_STORE_KEY)
            return

        restored_items.sort(key=lambda item: item.run_at)
        self._current_plan_day = datetime.now().astimezone().date()
        self._current_plan = restored_items
        logger.info(
            "[likelike] restored %s pending task(s) from plugin storage",
            len(restored_items),
        )
        await self.put_kv_data(self._PLAN_STORE_KEY, stored)

    async def _persist_plan(self) -> None:
        if self._current_plan_day is None:
            await self.delete_kv_data(self._PLAN_STORE_KEY)
            await self._delete_persistent_plan()
            return

        payload = {
            "plan_day": self._current_plan_day.isoformat(),
            "config": self._build_config_snapshot(),
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

    def _build_config_snapshot(self) -> dict[str, int | list[str]]:
        return {
            "qq_list": self._get_target_user_ids(),
            "like_times": self._get_like_times(),
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
        self._data_dir.mkdir(parents=True, exist_ok=True)
        with self._plan_file.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)

    def _unlink_plan_file(self) -> None:
        if self._plan_file.exists():
            self._plan_file.unlink()

    async def _send_like(self, user_id: str) -> bool:
        adapter = self._get_aiocqhttp_adapter()
        if adapter is None:
            logger.warning("[likelike] aiocqhttp adapter is not available")
            return False

        try:
            await adapter.bot.call_action(
                "send_like",
                user_id=user_id,
                times=self._get_like_times(),
            )
            logger.info("[likelike] sent like to %s", user_id)
            return True
        except ActionFailed as exc:
            logger.warning("[likelike] send_like failed for %s: %s", user_id, exc)
            return False
        except Exception as exc:
            logger.exception(
                "[likelike] unexpected send_like error for %s: %s", user_id, exc
            )
            return False

    def _get_aiocqhttp_adapter(self) -> AiocqhttpAdapter | None:
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
        return self._get_int_config("like_times", 10, min_value=1, max_value=20)

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
