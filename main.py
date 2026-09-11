import os
from typing import Any

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.provider import LLMResponse
from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.star.star_tools import StarTools
from .modules import ColdViolenceManager, MuteTracker, PokeReaction, WordFilter, ImageGenManager


@register("astrbot_plugin_zaxiang", "引灯续昼", "引灯续昼杂项插件", "1.0.0")
class ZaxiangPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.cold_violence_mgr = ColdViolenceManager()
        self.mute_tracker = MuteTracker()
        self.poke_reaction = PokeReaction()
        self.word_filter = WordFilter()
        self.image_gen = ImageGenManager()
        self.config = config or {}
    
    async def initialize(self):
        self.cold_violence_mgr.initialize(self.config)
        self.mute_tracker.initialize(self.config)
        self.poke_reaction.initialize(self.config)
        self.word_filter.initialize(self.config)
        self.image_gen.initialize(self.config)

        # 图片落盘目录：data/plugin_data/astrbot_plugin_zaxiang/images
        try:
            plugin_data_dir = StarTools.get_data_dir("astrbot_plugin_zaxiang")
            self.image_gen.set_data_dir(plugin_data_dir / "images")
        except Exception as e:
            logger.error(f"初始化图片保存目录失败: {e}")

        try:
            await self.image_gen.ensure_table(self.context.get_db())
        except Exception as e:
            logger.error(f"初始化图片生成数据表失败: {e}")

        # 注册复盘页面用到的 Web API
        self.context.register_web_api(
            "zaxiang_image_history/list", self._web_hist_list,
            ["GET"], "获取 AI 生成图片历史列表",
        )
        self.context.register_web_api(
            "zaxiang_image_history/file/<id>", self._web_hist_file,
            ["GET"], "获取单张 AI 生成图片",
        )
        self.context.register_web_api(
            "zaxiang_image_history/delete", self._web_hist_delete,
            ["POST"], "删除一条 AI 生成图片记录",
        )

        await self.cold_violence_mgr.start_cleanup_task()
        await self.image_gen.start_cleanup_task()
    
    async def terminate(self):
        await self.cold_violence_mgr.stop_cleanup_task()
        await self.image_gen.stop_cleanup_task()
    
    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse):
        """LLM 响应完成后、转换为消息链之前进行过滤（覆盖非流式输出场景）。"""
        if not self.word_filter.is_enabled():
            return
        text = response.completion_text
        if text:
            # completion_text 的 setter 会同步更新 result_chain 中的纯文本段
            response.completion_text = self.word_filter.filter_text(text)

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """发送消息前，将输出文本中的过滤词替换为固定词。"""
        if not self.word_filter.is_enabled():
            return
        result = event.get_result()
        if result is None or not result.chain:
            return
        for comp in result.chain:
            if isinstance(comp, Plain):
                comp.text = self.word_filter.filter_text(comp.text)
    
    async def _on_mute_lifted(self, event: AstrMessageEvent, mute_info: dict):
        curr_cid = await self.context.conversation_manager.get_curr_conversation_id(
            event.unified_msg_origin
        )
        conversation = None
        if curr_cid:
            conversation = await self.context.conversation_manager.get_conversation(
                event.unified_msg_origin, curr_cid
            )
        op_id = mute_info.get('operator_id', '')
        if op_id and op_id != '0':
            ban_desc = f"你刚刚被用户{op_id}禁言了{mute_info['duration_str']}"
        else:
            ban_desc = f"你刚刚被禁言了{mute_info['duration_str']}"
        yield event.request_llm(
            prompt="你已经可以说话了。",
            system_prompt=f"{ban_desc}，现在禁言刚被解除。请根据你的性格，自然地表达你的感受。",
            session_id=curr_cid or "",
            conversation=conversation,
        )
    
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        raw_message = event.message_obj.raw_message
        bot_id = event.message_obj.self_id

        if isinstance(raw_message, dict) and raw_message.get('notice_type') == 'group_ban':
            result = self.mute_tracker.process_notice_event(raw_message, bot_id)
            if result:
                async for resp in self._on_mute_lifted(event, result):
                    yield resp
            return

        if isinstance(raw_message, dict) and raw_message.get('sub_type') == 'poke':
            poke_result = self.poke_reaction.process_poke_event(raw_message, bot_id)
            if poke_result:
                if isinstance(event, AiocqhttpMessageEvent):
                    try:
                        await event.bot.api.call_action(
                            'send_poke',
                            group_id=int(poke_result['group_id']),
                            user_id=int(poke_result['target_id']),
                        )
                    except Exception:
                        pass
            return

        if not self.cold_violence_mgr.is_enabled():
            return
        
        self.cold_violence_mgr.cleanup_expired()
        
        sender_id = event.get_sender_id()
        
        if self.cold_violence_mgr.is_under_cold_violence(sender_id):
            messages = event.get_messages()
            group_id = event.message_obj.group_id
            
            at_bot = any(
                isinstance(msg, At) and str(msg.qq) == str(bot_id)
                for msg in messages
            )
            
            is_private = not group_id
            
            if at_bot or is_private:
                record = self.cold_violence_mgr.get_cold_violence_info(sender_id)
                if record:
                    remaining = self.cold_violence_mgr.format_remaining_time(record.remaining_time)
                    yield event.plain_result(
                        f"正在对{record.user_name}冷暴力，剩余时间 {remaining}"
                    )
                    return
    
    @filter.command("冷暴力")
    async def cold_violence_cmd(self, event: AstrMessageEvent, target: str = ""):
        sender_id = event.get_sender_id()
        
        if not self.cold_violence_mgr.has_authority(sender_id):
            yield event.plain_result("你没有权限,笨蛋")
            return
        
        if not self.cold_violence_mgr.is_enabled():
            yield event.plain_result("冷暴力功能未启用")
            return
        
        messages = event.get_messages()
        bot_id = event.message_obj.self_id
        target_id = None
        target_name = ""
        
        all_ats = [msg for msg in messages if isinstance(msg, At)]
        target_ats = [msg for msg in all_ats if str(msg.qq) != str(bot_id)]
        
        if len(target_ats) == 0:
            yield event.plain_result("你要我冷暴力谁啊？@一下对方")
            return
        else:
            target_at = target_ats[0]
            target_id = target_at.qq
            target_name = getattr(target_at, 'name', None) or str(target_id)
        
        if self.cold_violence_mgr.is_whitelisted(target_id):
            yield event.plain_result("可惜捏,你莫得权限")
            return
        
        if self.cold_violence_mgr.is_under_cold_violence(target_id):
            info = self.cold_violence_mgr.get_cold_violence_info(target_id)
            if info:
                remaining = self.cold_violence_mgr.format_remaining_time(info.remaining_time)
                yield event.plain_result(f"正在对{target_name}冷暴力，剩余时间 {remaining}")
            return
        
        if self.cold_violence_mgr.add_cold_violence(target_id, target_name):
            yield event.plain_result(f"已对{target_name} 实施冷暴力")
        else:
            yield event.plain_result("冷暴力失败")
    
    @filter.command("解除冷暴力")
    async def remove_cold_violence_cmd(self, event: AstrMessageEvent, target: str = ""):
        sender_id = event.get_sender_id()
        
        if not self.cold_violence_mgr.has_authority(sender_id):
            yield event.plain_result("你没有权限,笨蛋")
            return
        
        messages = event.get_messages()
        bot_id = event.message_obj.self_id
        target_id = None
        target_name = ""
        
        all_ats = [msg for msg in messages if isinstance(msg, At)]
        target_ats = [msg for msg in all_ats if str(msg.qq) != str(bot_id)]
        
        if len(target_ats) == 0:
            yield event.plain_result("你不@对方我怎么知道是谁？")
            return
        else:
            target_at = target_ats[0]
            target_id = target_at.qq
            target_name = getattr(target_at, 'name', None) or str(target_id)
        
        if self.cold_violence_mgr.remove_cold_violence(target_id):
            yield event.plain_result(f"已解除 {target_name} 的冷暴力")
        else:
            yield event.plain_result("又在造谣我冷暴力了昂，我现在没有冷暴力他捏")
    
    @filter.command("冷暴力列表")
    async def list_cold_violence_cmd(self, event: AstrMessageEvent):
        sender_id = event.get_sender_id()
        
        if not self.cold_violence_mgr.has_authority(sender_id):
            yield event.plain_result("你没有权限,笨蛋")
            return
        
        records = self.cold_violence_mgr.get_all_cold_violence_users()
        
        if not records:
            yield event.plain_result("当前没有人被冷暴力")
            return
        
        result = "当前冷暴力列表：\n"
        for record in records:
            remaining = self.cold_violence_mgr.format_remaining_time(record.remaining_time)
            result += f"- {record.user_name}：剩余 {remaining}\n"
        
        yield event.plain_result(result.strip())
    
    @filter.llm_tool(name="cold_violence_user")
    async def cold_violence_tool(self, event: AstrMessageEvent, user_id: str, user_name: str, duration: int = 10) -> MessageEventResult:
        '''冷暴力：AI对骚扰者实施冷处理，期间AI拒绝回复该用户任何消息。注意：这不是群禁言，被冷暴力的用户仍可在群里正常发言，只是AI不会理他。

        Args:
            user_id(string): 要冷暴力的用户ID
            user_name(string): 要冷暴力的用户名称
            duration(number): 冷暴力持续时长（分钟），默认10分钟
        '''
        if not self.cold_violence_mgr.is_enabled():
            yield event.plain_result("冷暴力功能未启用")
            return
        
        if self.cold_violence_mgr.is_whitelisted(user_id):
            yield event.plain_result(f"{user_name} 在白名单中，无法冷暴力")
            return
        
        if self.cold_violence_mgr.is_under_cold_violence(user_id):
            info = self.cold_violence_mgr.get_cold_violence_info(user_id)
            if info:
                remaining = self.cold_violence_mgr.format_remaining_time(info.remaining_time)
                yield event.plain_result(f"正在对{user_name}冷暴力中，剩余 {remaining}")
            return
        
        if self.cold_violence_mgr.add_cold_violence(user_id, user_name, duration):
            yield event.plain_result(f"已对 {user_name} 实施冷暴力，时长 {duration} 分钟")
        else:
            yield event.plain_result(f"冷暴力失败")
    
    @filter.llm_tool(name="remove_cold_violence_user")
    async def remove_cold_violence_tool(self, event: AstrMessageEvent, user_id: str, user_name: str) -> MessageEventResult:
        '''解除冷暴力：恢复AI对指定用户的正常回复。注意：这不是解除群禁言，冷暴力只是AI不理他，解除后AI会重新回复他。

        Args:
            user_id(string): 要解除冷暴力的用户ID
            user_name(string): 要解除冷暴力的用户名称
        '''
        if not self.cold_violence_mgr.is_enabled():
            yield event.plain_result("冷暴力功能未启用")
            return
        
        if self.cold_violence_mgr.remove_cold_violence(user_id):
            yield event.plain_result(f"已解除 {user_name} 的冷暴力")
        else:
            yield event.plain_result(f"{user_name} 未被冷暴力")
    
    @filter.llm_tool(name="check_cold_violence_status")
    async def check_cold_violence_tool(self, event: AstrMessageEvent, user_id: str, user_name: str) -> MessageEventResult:
        '''查询冷暴力状态：查看指定用户是否正在被AI冷暴力及剩余时间。注意：冷暴力是AI不理他，不是群禁言。

        Args:
            user_id(string): 要查询的用户ID
            user_name(string): 要查询的用户名称
        '''
        if not self.cold_violence_mgr.is_enabled():
            yield event.plain_result("冷暴力功能未启用")
            return
        
        if self.cold_violence_mgr.is_under_cold_violence(user_id):
            record = self.cold_violence_mgr.get_cold_violence_info(user_id)
            if record:
                remaining = self.cold_violence_mgr.format_remaining_time(record.remaining_time)
                yield event.plain_result(
                    f"正在对{user_name}冷暴力 ，解冻时间 {remaining}"
                )
        else:
            yield event.plain_result(f"{user_name} 未被冷暴力")

    # ---------------- AI 绘图（文生图 / 图生图） ----------------

    @filter.llm_tool(name="generate_image")
    async def generate_image_tool(
        self, event: AstrMessageEvent, prompt: str, size: str = ""
    ) -> MessageEventResult:
        '''根据用户的描述生成一张全新的图片，并自动把图片发给用户。仅当用户明确要求画图/绘画/生成图片/想象一张图时调用，普通聊天不要调用。为用户斟酌一个具体、形象的描述，使用英文效果通常更好。

        Args:
            prompt(string): 详细的图片描述，应包含主体、场景、整体风格、光线、氛围等，越具体越好。
            size(string): 可选尺寸 1024x1024、1024x1536、1536x1024，不知道就留空。
        '''
        mgr = self.image_gen
        if not mgr.config.get('enabled', True):
            yield "绘图功能当前未启用。"
            return
        if not prompt or not str(prompt).strip():
            yield "缺少图片描述，无法生成。"
            return
        sender_id, group_id, session_id = mgr.extract_sender_info(event)
        try:
            result = await mgr.generate(str(prompt).strip(), size)
        except Exception as e:
            logger.error(f"文生图失败: {e}")
            # 只把失败原因回传给 LLM，由 LLM 决定怎么跟用户说，不直接发固定文案
            yield f"图片生成失败：{e}"
            return
        try:
            await mgr.add_record(
                self.context.get_db(), mode='t2i', prompt=str(prompt).strip(),
                revised_prompt=result['revised_prompt'], image_path=result['path'],
                sender_id=sender_id, group_id=group_id, session_id=session_id,
            )
        except Exception as e:
            logger.warning(f"记录生成图片到数据库失败: {e}")
        # 直接发图给用户（经实测 event.send 发本地文件最可靠）
        await event.send(MessageChain([Image.fromFileSystem(result['path'])]))
        # 回传一句状态给 LLM，让它自己接话，不直接发固定文案
        yield "图片已经生成并直接发送给用户了。"

    @filter.llm_tool(name="edit_image")
    async def edit_image_tool(
        self, event: AstrMessageEvent, prompt: str, size: str = ""
    ) -> MessageEventResult:
        '''基于用户消息里发送的图片进行第二次创作（图生图）：参考用户当条消息附带的图片，按描述修改/重绘它。仅当用户明确要求"把这张图改成…"/"基于这张图再画…"时调用，需要用户刚刚发过图片。

        Args:
            prompt(string): 希望如何修改或重绘这张图的描述，具体且形象，英文效果通常更好。
            size(string): 可选尺寸 1024x1024、1024x1536、1536x1024，不知道就留空。
        '''
        mgr = self.image_gen
        if not mgr.config.get('enabled', True):
            yield "绘图功能当前未启用。"
            return
        if not prompt or not str(prompt).strip():
            yield "缺少修改描述，无法处理。"
            return
        ref_path = await mgr.resolve_reference_image(event)
        if not ref_path:
            yield "当前消息里没有找到图片，需要用户先发一张图才能基于它修改。"
            return
        sender_id, group_id, session_id = mgr.extract_sender_info(event)
        try:
            result = await mgr.edit(str(prompt).strip(), ref_path, size)
        except Exception as e:
            logger.error(f"图生图失败: {e}")
            # 只把失败原因回传给 LLM，由 LLM 决定怎么跟用户说
            yield f"图片修改失败：{e}"
            return
        try:
            await mgr.add_record(
                self.context.get_db(), mode='i2i', prompt=str(prompt).strip(),
                revised_prompt=result['revised_prompt'], image_path=result['path'],
                sender_id=sender_id, group_id=group_id, session_id=session_id,
            )
        except Exception as e:
            logger.warning(f"记录修改图片到数据库失败: {e}")
        # 直接发图给用户（经实测 event.send 发本地文件最可靠）
        await event.send(MessageChain([Image.fromFileSystem(result['path'])]))
        # 回传一句状态给 LLM，让它自己接话，不直接发固定文案
        yield "图片已经修改并直接发送给用户了。"

    # ---------------- 复盘 Web API ----------------

    @staticmethod
    def _make_thumb_data_url(image_path: str, size: int = 256) -> str:
        """把图片转成缩略图 base64 data URL，供复盘页面展示。失败返回空串。"""
        try:
            from PIL import Image as PILImage
            from io import BytesIO
            import base64 as b64
            if not os.path.exists(image_path):
                return ""
            img = PILImage.open(image_path)
            img.thumbnail((size, size))
            buf = BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=70)
            data = b64.b64encode(buf.getvalue()).decode()
            return f"data:image/jpeg;base64,{data}"
        except Exception:
            return ""

    @staticmethod
    def _make_full_data_url(image_path: str) -> str:
        """把原图转成 base64 data URL。失败返回空串。"""
        try:
            import base64 as b64
            if not os.path.exists(image_path):
                return ""
            with open(image_path, "rb") as f:
                data = b64.b64encode(f.read()).decode()
            return f"data:image/png;base64,{data}"
        except Exception:
            return ""

    async def _web_hist_list(self) -> Any:
        try:
            records = await self.image_gen.list_records(self.context.get_db())
        except Exception as e:
            logger.error(f"读取生成历史失败: {e}")
            return error_response("读取历史失败")
        items = []
        for r in records:
            items.append(
                {
                    "id": r["id"],
                    "mode": r["mode"],
                    "prompt": r["prompt"],
                    "revised_prompt": r["revised_prompt"],
                    "created_at": r["created_at"],
                    "expires_at": r["expires_at"],
                    "thumb": self._make_thumb_data_url(r["image_path"]),
                }
            )
        # bridge 兼容：返回业务 JSON 对象而非 list
        return json_response({"records": items})

    async def _web_hist_file(self, id) -> Any:
        try:
            rec = await self.image_gen.get_record(self.context.get_db(), int(id))
        except (TypeError, ValueError):
            return error_response("参数错误", status_code=400)
        if not rec or not rec.get("image_path"):
            return error_response("记录不存在", status_code=404)
        data_url = self._make_full_data_url(rec["image_path"])
        if not data_url:
            return error_response("图片不存在或已过期", status_code=404)
        return json_response({"data_url": data_url})

    async def _web_hist_delete(self) -> Any:
        try:
            payload = await request.json(default={})
            record_id = int(payload.get("id") or 0)
        except (TypeError, ValueError):
            return error_response("参数错误", status_code=400)
        if record_id <= 0:
            return error_response("参数错误", status_code=400)
        try:
            ok = await self.image_gen.delete_record(self.context.get_db(), record_id)
        except Exception as e:
            logger.error(f"删除生成记录失败: {e}")
            return error_response("删除失败")
        if not ok:
            return error_response("记录不存在", status_code=404)
        return json_response({"deleted": True})
