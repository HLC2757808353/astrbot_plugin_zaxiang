import os
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.provider import LLMResponse
from astrbot.api import logger
from astrbot.api.web import PluginUploadFile, error_response, json_response, request
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.star.star_tools import StarTools
from .modules import (
    ColdViolenceManager, MuteTracker, PokeReaction, WordFilter,
    ImageGenManager, FileSenderManager, MoneyManager,
)


@register("astrbot_plugin_zaxiang", "引灯续昼", "引灯续昼杂项插件", "1.0.0")
class ZaxiangPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.cold_violence_mgr = ColdViolenceManager()
        self.mute_tracker = MuteTracker()
        self.poke_reaction = PokeReaction()
        self.word_filter = WordFilter()
        self.image_gen = ImageGenManager()
        self.file_sender = FileSenderManager()
        self.money = MoneyManager()
        self.config = config or {}
    
    async def initialize(self):
        self.cold_violence_mgr.initialize(self.config)
        self.mute_tracker.initialize(self.config)
        self.poke_reaction.initialize(self.config)
        self.word_filter.initialize(self.config)
        self.image_gen.initialize(self.config)
        self.file_sender.initialize(self.config)
        self.money.initialize(self.config)

        # 插件数据目录：data/plugin_data/astrbot_plugin_zaxiang
        try:
            plugin_data_dir = StarTools.get_data_dir("astrbot_plugin_zaxiang")
            self.image_gen.set_data_dir(plugin_data_dir / "images")
            self.money.set_data_dir(plugin_data_dir)
            # 禁言记录落盘：重载/重启后仍知道是谁禁言的
            self.mute_tracker.set_state_path(plugin_data_dir / "mute_state.json")
        except Exception as e:
            logger.error(f"初始化插件数据目录失败: {e}")

        try:
            await self.image_gen.ensure_table(self.context.get_db())
        except Exception as e:
            logger.error(f"初始化图片生成数据表失败: {e}")

        # 启动时立刻清一次，避免重启后要等一个小时才处理已过期的图片
        try:
            await self.image_gen.cleanup_expired(self.context.get_db())
            await self.image_gen.cleanup_orphan_files(self.context.get_db())
        except Exception as e:
            logger.error(f"启动时清理过期图片失败: {e}")

        # 流式输出会绕过插件的结果装饰钩子（ResultDecorateStage 对 STREAMING_RESULT
        # 直接 return），因此输出替换词在流式模式下不会生效，这里提前给出提示。
        try:
            provider_settings = self.context.get_config().get("provider_settings", {})
            if provider_settings.get("streaming_response"):
                logger.warning(
                    "检测到已开启 LLM 流式输出：流式模式下 AstrBot 不会调用插件的 "
                    "on_decorating_result 钩子，输出替换词（word_filter）可能不生效。"
                    "如需替换词生效，请在 WebUI 中关闭「流式输出」。"
                )
        except Exception as e:
            logger.debug(f"读取全局配置失败: {e}")

        # 注册插件页面用到的 Web API（路由必须带插件名前缀）
        # 用 try 包住：老版本 AstrBot 没有 register_web_api 时，只该丢失面板页面，
        # 不应该让整个插件（替换词、禁言提示等）加载失败。
        try:
            self.context.register_web_api(
                "astrbot_plugin_zaxiang/image_history/list", self._web_hist_list,
                ["GET"], "获取 AI 生成图片历史列表",
            )
            self.context.register_web_api(
                "astrbot_plugin_zaxiang/image_history/file/<id>", self._web_hist_file,
                ["GET"], "获取单张 AI 生成图片",
            )
            self.context.register_web_api(
                "astrbot_plugin_zaxiang/image_history/delete", self._web_hist_delete,
                ["POST"], "删除一条 AI 生成图片记录",
            )
            self.context.register_web_api(
                "astrbot_plugin_zaxiang/qrcode/info", self._web_qrcode_info,
                ["GET"], "获取收款二维码信息",
            )
            self.context.register_web_api(
                "astrbot_plugin_zaxiang/qrcode/upload", self._web_qrcode_upload,
                ["POST"], "上传收款二维码",
            )
            self.context.register_web_api(
                "astrbot_plugin_zaxiang/qrcode/delete", self._web_qrcode_delete,
                ["POST"], "删除收款二维码",
            )
        except Exception as e:
            logger.error(
                f"注册插件 Web API 失败（面板页面将不可用，其他功能不受影响）: {e}"
            )

        await self.cold_violence_mgr.start_cleanup_task()
        # 必须把 get_db 传进去，否则清理任务拿不到数据库、过期图片永远清不掉
        await self.image_gen.start_cleanup_task(self.context.get_db)
    
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
    
    async def _member_display_name(
        self, event: AstrMessageEvent, group_id: str, user_id: str
    ) -> str:
        """查询群成员的群名片/昵称，用于禁言提示里说出具体是谁。失败返回空串。

        Args:
            event: 当前事件（需为 aiocqhttp 平台事件才能调用群成员接口）。
            group_id: 群号。
            user_id: 目标 QQ。

        Returns:
            群名片优先，其次昵称；查询失败返回空字符串。
        """
        if not isinstance(event, AiocqhttpMessageEvent) or not group_id or not user_id:
            return ""
        try:
            info = await event.bot.api.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=True,
            )
        except Exception as e:
            logger.debug(f"获取群成员信息失败({group_id}/{user_id}): {e}")
            return ""
        if not isinstance(info, dict):
            return ""
        return str(info.get("card") or info.get("nickname") or "")

    async def _on_mute_lifted(self, event: AstrMessageEvent, mute_info: dict):
        curr_cid = await self.context.conversation_manager.get_curr_conversation_id(
            event.unified_msg_origin
        )
        conversation = None
        if curr_cid:
            conversation = await self.context.conversation_manager.get_conversation(
                event.unified_msg_origin, curr_cid
            )

        op_id = mute_info.get("operator_id", "")
        op_name = mute_info.get("operator_name", "")
        if op_name and op_id:
            who = f"{op_name}（QQ {op_id}）"
        elif op_id:
            who = f"QQ {op_id} 的管理员"
        else:
            who = "一位群管理员（具体是谁没查到，可能是全员禁言或系统操作）"

        lines = [
            f"你刚刚在这个群里被 {who} 禁言了 "
            f"{mute_info.get('duration_str', '')}，现在禁言已经解除，你又可以说话了。"
        ]

        lift_name = mute_info.get("lift_operator_name") or ""
        lift_id = mute_info.get("lift_operator_id") or ""
        if lift_name and lift_id:
            lines.append(f"这次是 {lift_name}（QQ {lift_id}）给你解开的。")
        elif lift_name:
            lines.append(f"这次是 {lift_name} 给你解开的。")
        elif lift_id:
            lines.append(f"这次是 QQ {lift_id} 给你解开的。")
        elif mute_info.get("auto_lifted"):
            lines.append("看起来是禁言时间到了、自动解除的。")
        else:
            lines.append("禁言已经结束了。")

        yield event.request_llm(
            prompt="（禁言已解除，你现在可以正常说话了）",
            system_prompt=(
                "\n".join(lines)
                + "\n请用你自己的性格和口吻，简短地说说被禁言这件事的感受，"
                "可以吐槽禁言你的人，也可以装作若无其事。"
                "不要提及系统提示，也不要把上面这段说明照抄出来。"
            ),
            session_id=curr_cid or "",
            conversation=conversation,
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        raw_message = event.message_obj.raw_message
        bot_id = event.message_obj.self_id
        # 兼容 dict 与 aiocqhttp Event（dict 子类 / 带 get 的对象）
        getter = getattr(raw_message, "get", None)

        if callable(getter) and getter("notice_type") == "group_ban":
            result = self.mute_tracker.process_notice_event(raw_message, bot_id)
            if result and result.get("type") == "ban":
                # 立刻补全操作者昵称，解禁时才能说出"被谁禁言"
                if result.get("operator_id"):
                    name = await self._member_display_name(
                        event, result["group_id"], result["operator_id"]
                    )
                    self.mute_tracker.update_operator_name(result["group_id"], name)
                duration_text = (
                    "永久" if not result["duration"] else f"{result['duration']} 秒"
                )
                logger.info(
                    f"机器人被禁言：群 {result['group_id']}，操作者 "
                    f"{result.get('operator_id') or '未知'}，时长 {duration_text}"
                )
                event.should_call_llm(True)
            elif result and result.get("type") == "lift":
                if result.get("operator_id"):
                    name = await self._member_display_name(
                        event, result["group_id"], result["operator_id"]
                    )
                    if name:
                        result["operator_name"] = name
                if result.get("lift_operator_id"):
                    result["lift_operator_name"] = await self._member_display_name(
                        event, result["group_id"], result["lift_operator_id"]
                    )
                logger.info(
                    f"机器人禁言解除：群 {result['group_id']}，禁言者 "
                    f"{result.get('operator_name') or result.get('operator_id') or '未知'}，"
                    f"时长 {result.get('duration_str')}"
                )
                async for resp in self._on_mute_lifted(event, result):
                    yield resp
                # 已经用插件自己的 LLM 请求回复过了，避免默认 LLM 链路再回一次
                event.should_call_llm(True)
            return

        if callable(getter) and getter("sub_type") == "poke":
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
        """把图片转成 base64 data URL（mime 按扩展名判断）。失败返回空串。"""
        try:
            import base64 as b64
            import mimetypes
            if not os.path.exists(image_path):
                return ""
            mime = mimetypes.guess_type(image_path)[0] or "image/png"
            with open(image_path, "rb") as f:
                data = b64.b64encode(f.read()).decode()
            return f"data:{mime};base64,{data}"
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

    # ---------------- 文件发送 ----------------

    @filter.llm_tool(name="send_file")
    async def send_file_tool(
        self, event: AstrMessageEvent, path: str, description: str = ""
    ) -> MessageEventResult:
        '''把本地文件直接发送给用户。适用于用户需要接收代码文件、md/文本、配置文件、图片、音视频等场景，或者你把文件下载/生成本地文件后需要交给用户时。文件必须存在于本机，且大小不能超过限制（默认 10MB，可在插件配置里调整）。

        Args:
            path(string): 要发送的文件的完整本地路径（绝对路径），例如 /tmp/xxx.py 或 C:/xxx/xxx.md。路径前可带 file:// 前缀。
            description(string): 对文件内容的简短说明（可选），可帮助用户了解这是什么文件。
        '''
        mgr = self.file_sender
        if not mgr.is_enabled():
            yield "文件发送功能当前未启用。"
            return
        if not path or not str(path).strip():
            yield "缺少要发送的文件路径。"
            return
        # 兼容 file:// 前缀
        raw_path = str(path).strip()
        if raw_path.startswith("file://"):
            from urllib.parse import unquote, urlparse
            raw_path = unquote(urlparse(raw_path).path)
        err = await mgr.send(event, raw_path)
        if err:
            # 只把错误原因回传给 LLM，由 LLM 决定怎么跟用户说
            yield err
            return
        yield "文件已经直接发送给用户了。" + (f"（说明：{description}）" if description else "")

    # ---------------- 收款码 Web API ----------------

    async def _web_qrcode_info(self) -> Any:
        path = self.money.find_qrcode()
        if not path:
            return json_response({"exists": False})
        try:
            stat = path.stat()
        except OSError:
            return json_response({"exists": False})
        return json_response(
            {
                "exists": True,
                "filename": path.name,
                "size": stat.st_size,
                "uploaded_at": datetime.fromtimestamp(stat.st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "preview": self._make_full_data_url(str(path)),
            }
        )

    async def _web_qrcode_upload(self) -> Any:
        files = await request.files()
        upload = files.get("file")
        if not isinstance(upload, PluginUploadFile):
            return error_response("缺少上传文件", status_code=400)
        if not self.money.data_dir:
            return error_response("插件数据目录未初始化", status_code=500)

        ext = Path(upload.filename or "").suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            return error_response("仅支持 png/jpg/jpeg/webp 格式的图片", status_code=400)

        try:
            data = await upload.read(-1)
        except Exception as e:
            logger.error(f"读取上传文件失败: {e}")
            return error_response("读取上传文件失败", status_code=400)
        if not data:
            return error_response("上传文件为空", status_code=400)
        if len(data) > 5 * 1024 * 1024:
            return error_response("图片过大，请上传 5MB 以内的二维码图片", status_code=400)

        # 确认是有效图片
        try:
            from io import BytesIO
            from PIL import Image as PILImage

            PILImage.open(BytesIO(data)).verify()
        except Exception:
            return error_response("文件不是有效的图片", status_code=400)

        target = self.money.data_dir / f"qrcode{ext}"
        try:
            # 清理其他格式的旧二维码，避免探测到多份
            for old_ext in (".png", ".jpg", ".jpeg", ".webp"):
                old = self.money.data_dir / f"qrcode{old_ext}"
                if old != target and old.is_file():
                    old.unlink()
            target.write_bytes(data)
        except OSError as e:
            logger.error(f"保存二维码失败: {e}")
            return error_response("保存二维码失败", status_code=500)

        logger.info(f"收款二维码已更新：{target}")
        return json_response({"saved": True, "filename": target.name})

    async def _web_qrcode_delete(self) -> Any:
        path = self.money.find_qrcode()
        if not path:
            return error_response("当前没有已上传的二维码", status_code=404)
        try:
            path.unlink()
        except OSError as e:
            logger.error(f"删除二维码失败: {e}")
            return error_response("删除失败", status_code=500)
        logger.info(f"收款二维码已删除：{path}")
        return json_response({"deleted": True})

    # ---------------- 挣钱（收款码） ----------------

    @filter.llm_tool(name="earn_money")
    async def earn_money_tool(
        self, event: AstrMessageEvent, amount: str = "", note: str = ""
    ) -> MessageEventResult:
        '''把预先配置好的收款二维码发给用户。仅当用户明确提出要给你钱、打赏、赞助、请你喝奶茶、让你去"挣钱"这类意图时才调用；严禁在用户没有明确要求时主动发送，也不要自行发起索要钱财的话题。

        Args:
            amount(string): 用户提到的金额，例如"50"或"50块"，没提到就留空。
            note(string): 可选的补充说明，用来给你自己稍后组织语言提供素材。
        '''
        mgr = self.money
        if not mgr.is_enabled():
            yield "挣钱功能当前未启用。"
            return
        if not mgr.has_permission(event.get_sender_id()):
            yield "当前用户没有触发挣钱功能的权限。"
            return
        err = await mgr.send_qrcode(event)
        if err:
            # 只把原因回传给 LLM，由 LLM 决定怎么跟用户说
            yield err
            return
        info = "收款二维码已经直接发送给用户了。"
        if amount:
            info += f"用户提到的金额：{amount}。"
        if note:
            info += f"补充素材：{note}。"
        yield info
