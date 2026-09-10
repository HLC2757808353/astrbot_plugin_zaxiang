"""AI 绘图模块。

文生图 / 图生图，由 LLM 通过 llm_tool 自然语言触发，生成图片后直接发送给用户，
并记录到数据库（默认保留 7 天，每日扫描删除过期记录与文件）。
"""

import asyncio
import base64
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.core.message.components import Image as ImageComponent
from sqlalchemy import text

try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore[assignment]


class ImageGenManager:
    """管理绘图 API 调用、本地落盘、数据库记录与过期清理。"""

    DEFAULT_CONFIG = {
        "enabled": True,
        "api_base": "https://one.aznb.top/v1",
        "api_key": "sk-jf8e07U1t9wXMwOgrrJzYZaTJE4J3FbaQxqeB39B69w8zKEg",
        "model": "gpt-image-2.5-flare",
        "size": "1024x1024",
        "retention_days": 7,
    }

    # 尺寸白名单，防止 LLM 乱填
    _SIZE_WHITELIST = {"1024x1024", "1024x1536", "1536x1024"}

    def __init__(self):
        self.config: dict = self.DEFAULT_CONFIG.copy()
        self._cleanup_task: Optional[asyncio.Task] = None
        self.data_dir: Optional[Path] = None

    # ---------- 配置与生命周期 ----------

    def initialize(self, config: dict):
        cfg = config.get("image_gen", config)
        merged = {**self.DEFAULT_CONFIG, **cfg}
        merged["api_base"] = str(merged.get("api_base") or "").strip().rstrip("/")
        merged["api_key"] = str(merged.get("api_key") or "").strip()
        merged["model"] = str(merged.get("model") or self.DEFAULT_CONFIG["model"]).strip()
        merged["size"] = str(merged.get("size") or self.DEFAULT_CONFIG["size"]).strip()
        self.config = merged
        try:
            self.config["retention_days"] = max(1, int(self.config.get("retention_days", 7)))
        except (TypeError, ValueError):
            self.config["retention_days"] = 7
        logger.info("图片生成模块初始化完成")

    def set_data_dir(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    async def start_cleanup_task(self):
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
            logger.info("图片生成清理任务已启动")

    async def stop_cleanup_task(self):
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            logger.info("图片生成清理任务已停止")

    async def _periodic_cleanup(self):
        while True:
            try:
                await asyncio.sleep(24 * 3600)
                await self.cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"图片生成清理任务出错: {e}")

    # ---------- 工具入口 ----------

    async def generate(self, prompt: str, size: str = "") -> dict:
        """文生图。返回 {"path": ..., "revised_prompt": ...}，失败抛出异常。"""
        size = self._normalize_size(size)
        payload = {
            "model": self.config["model"],
            "prompt": prompt,
            "n": 1,
            "size": size,
        }
        resp = await self._call_api("/images/generations", payload=payload)
        return await self._handle_image_response(resp)

    async def edit(self, prompt: str, image_path: str, size: str = "") -> dict:
        """图生图。image_path 为参考图本地路径，multipart 提交。"""
        size = self._normalize_size(size)
        data = {
            "model": self.config["model"],
            "prompt": prompt,
            "n": 1,
            "size": size,
        }
        files = {"image": ("image.png", open(image_path, "rb"), "image/png")}
        try:
            resp = await self._call_api("/images/edits", data=data, files=files)
        finally:
            files["image"][1].close()
        return await self._handle_image_response(resp)

    # ---------- 内部实现 ----------

    def _normalize_size(self, size: str) -> str:
        size = (size or "").strip().lower().replace(" ", "")
        if size not in self._SIZE_WHITELIST:
            return self.config["size"]
        return size

    async def _call_api(self, endpoint: str, *, payload=None, data=None, files=None) -> dict:
        """调用第三方绘图 API，返回解析后的 JSON dict。"""
        if aiohttp is None:
            raise RuntimeError("aiohttp 未安装，无法调用绘图 API")
        api_base = self.config["api_base"]
        api_key = self.config["api_key"]
        if not api_base or not api_key:
            raise RuntimeError("绘图 API 未配置（api_base/api_key 为空）")

        url = f"{api_base}{endpoint}"
        headers = {"Authorization": f"Bearer {api_key}"}

        timeout = aiohttp.ClientTimeout(total=600)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if files:
                async with session.post(url, headers=headers, data=data, files=files) as resp:
                    body = await resp.text()
            elif payload is not None:
                headers["Content-Type"] = "application/json"
                async with session.post(url, headers=headers, json=payload) as resp:
                    body = await resp.text()
            else:
                raise ValueError("_call_api 需要 payload 或 files")

            if resp.status != 200:
                logger.error(f"绘图 API 返回 {resp.status}: {body[:500]}")
                raise RuntimeError(f"绘图 API 返回错误状态 {resp.status}")
            try:
                return json.loads(body)
            except json.JSONDecodeError as e:
                logger.error(f"绘图 API 响应不是合法 JSON: {body[:200]}")
                raise RuntimeError("绘图 API 响应解析失败") from e

    async def _handle_image_response(self, resp: dict) -> dict:
        data = resp.get("data") or []
        if not data or not isinstance(data, list):
            raise RuntimeError("绘图 API 返回数据为空")
        item = data[0]
        b64 = item.get("b64_json")
        if not b64:
            raise RuntimeError("绘图 API 未返回 b64_json")
        image_bytes = base64.b64decode(b64)
        path = await self._save_image(image_bytes)
        revised = item.get("revised_prompt") or ""
        return {"path": str(path), "revised_prompt": revised}

    async def _save_image(self, image_bytes: bytes) -> Path:
        if not self.data_dir:
            raise RuntimeError("图片保存目录未初始化")
        filename = f"{time.strftime('%Y%m%d')}_{uuid.uuid4().hex[:12]}.png"
        path = self.data_dir / filename
        await asyncio.to_thread(path.write_bytes, image_bytes)
        return path

    # ---------- 数据库记录 ----------

    async def _run_sql(self, db, sql: str, params: Optional[dict] = None):
        async with db.get_db() as session:
            await session.execute(text(sql), params or {})
            await session.commit()

    async def ensure_table(self, db):
        sql = """
        CREATE TABLE IF NOT EXISTS image_gen_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL,
            prompt TEXT NOT NULL,
            revised_prompt TEXT DEFAULT '',
            size TEXT DEFAULT '',
            image_path TEXT NOT NULL,
            sender_id TEXT DEFAULT '',
            group_id TEXT DEFAULT '',
            session_id TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """
        await self._run_sql(db, sql)

    async def add_record(self, db, *, mode, prompt, revised_prompt, image_path, sender_id="", group_id="", session_id=""):
        now = time.time()
        created = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        expires = datetime.fromtimestamp(
            now + self.config["retention_days"] * 86400, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        await self._run_sql(
            db,
            """INSERT INTO image_gen_records
               (mode, prompt, revised_prompt, size, image_path, sender_id, group_id, session_id, created_at, expires_at)
               VALUES (:mode, :prompt, :revised_prompt, :size, :image_path, :sender_id, :group_id, :session_id, :created_at, :expires_at)""",
            {
                "mode": mode,
                "prompt": prompt,
                "revised_prompt": revised_prompt,
                "size": self.config["size"],
                "image_path": str(image_path),
                "sender_id": sender_id or "",
                "group_id": group_id or "",
                "session_id": session_id or "",
                "created_at": created,
                "expires_at": expires,
            },
        )

    async def cleanup_expired(self, db=None):
        """删除已过期的记录和对应图片文件。"""
        if db is None:
            return
        await self.ensure_table(db)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        async with db.get_db() as session:
            result = await session.execute(
                text("SELECT id, image_path FROM image_gen_records WHERE expires_at <= :now"),
                {"now": now},
            )
            rows = result.fetchall()
            for row in rows:
                rid, image_path = row
                try:
                    if image_path and os.path.exists(image_path):
                        os.remove(image_path)
                except OSError as e:
                    logger.warning(f"删除过期图片失败 {image_path}: {e}")
                await session.execute(
                    text("DELETE FROM image_gen_records WHERE id = :rid"),
                    {"rid": rid},
                )
            await session.commit()
        if rows:
            logger.info(f"图片生成清理：删除 {len(rows)} 条过期记录")

    async def list_records(self, db):
        """返回记录列表 [{id, mode, prompt, created_at, expires_at, ...}]，按创建时间倒序。"""
        await self.ensure_table(db)
        async with db.get_db() as session:
            result = await session.execute(
                text("""
                    SELECT id, mode, prompt, revised_prompt, size, image_path,
                           sender_id, group_id, created_at, expires_at
                    FROM image_gen_records
                    ORDER BY id DESC
                    LIMIT 200
                """)
            )
            cols = ["id", "mode", "prompt", "revised_prompt", "size", "image_path",
                    "sender_id", "group_id", "created_at", "expires_at"]
            records = [dict(zip(cols, row)) for row in result.fetchall()]
        return records

    async def get_record(self, db, record_id: int):
        await self.ensure_table(db)
        async with db.get_db() as session:
            row = (await session.execute(
                text("SELECT * FROM image_gen_records WHERE id = :id"),
                {"id": int(record_id)},
            )).fetchone()
            if not row:
                return None
            return {key: value for key, value in row._mapping.items()}

    async def delete_record(self, db, record_id: int):
        """删除记录并清理本地文件，返回布尔是否删除成功。"""
        await self.ensure_table(db)
        found = await self.get_record(db, record_id)
        if not found:
            return False
        image_path = found.get("image_path")
        try:
            if image_path and os.path.exists(image_path):
                os.remove(image_path)
        except OSError as e:
            logger.warning(f"删除图片文件失败 {image_path}: {e}")
        await self._run_sql(db, "DELETE FROM image_gen_records WHERE id = :id", {"id": int(record_id)})
        return True

    # ---------- 参考图解析 ----------

    async def resolve_reference_image(self, event) -> Optional[str]:
        """从当前消息的 Image 组件中取参考图，转为本地文件路径。"""
        messages = event.get_messages()
        for comp in messages:
            if isinstance(comp, ImageComponent):
                try:
                    path = await comp.convert_to_file_path()
                    if path:
                        return path
                except Exception as e:
                    logger.warning(f"解析参考图失败: {e}")
                    continue
        return None

    def extract_sender_info(self, event) -> tuple:
        """返回 (sender_id, group_id, session_id)。"""
        try:
            sender_id = event.get_sender_id() or ""
        except Exception:
            sender_id = ""
        group_id = ""
        session_id = ""
        try:
            group_id = getattr(event.message_obj, "group_id", None) or ""
        except Exception:
            pass
        try:
            session_id = getattr(event, "unified_msg_origin", "") or ""
        except Exception:
            pass
        return str(sender_id), str(group_id), str(session_id)
