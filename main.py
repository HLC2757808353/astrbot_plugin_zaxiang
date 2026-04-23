from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import At
from .modules import ColdViolenceManager


@register("astrbot_plugin_zaxiang", "引灯续昼", "引灯续昼杂项插件", "1.0.0")
class ZaxiangPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.cold_violence_mgr = ColdViolenceManager()
    
    async def initialize(self):
        config = self.context.get_config()
        self.cold_violence_mgr.initialize(config)
        await self.cold_violence_mgr.start_cleanup_task()
        logger.info("引灯续昼杂项插件初始化完成")
    
    async def terminate(self):
        await self.cold_violence_mgr.stop_cleanup_task()
        logger.info("引灯续昼杂项插件已终止")
    
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if not self.cold_violence_mgr.is_enabled():
            return
        
        sender_id = event.get_sender_id()
        
        if self.cold_violence_mgr.is_under_cold_violence(sender_id):
            record = self.cold_violence_mgr.get_cold_violence_info(sender_id)
            if record:
                remaining = self.cold_violence_mgr.format_remaining_time(record.remaining_time)
                yield event.plain_result(
                    f"正在冷暴力 {record.user_name}，剩余时间 {remaining}"
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
        target_id = None
        target_name = ""
        
        for msg in messages:
            if isinstance(msg, At):
                target_id = msg.qq
                target_name = f"用户{target_id}"
                break
        
        if not target_id:
            yield event.plain_result("你要我冷暴力谁啊？@一下对方")
            return
        
        if self.cold_violence_mgr.is_whitelisted(target_id):
            yield event.plain_result("可惜捏,你莫得权限")
            return
        
        if self.cold_violence_mgr.add_cold_violence(target_id, target_name):
            yield event.plain_result(f"已对 {target_name} 实施冷暴力")
        else:
            yield event.plain_result("冷暴力失败")
    
    @filter.command("解除冷暴力")
    async def remove_cold_violence_cmd(self, event: AstrMessageEvent, target: str = ""):
        sender_id = event.get_sender_id()
        
        if not self.cold_violence_mgr.has_authority(sender_id):
            yield event.plain_result("你没有权限,笨蛋")
            return
        
        messages = event.get_messages()
        target_id = None
        target_name = ""
        
        for msg in messages:
            if isinstance(msg, At):
                target_id = msg.qq
                target_name = f"用户{target_id}"
                break
        
        if not target_id:
            yield event.plain_result("你不@对方我怎么知道是谁？")
            return
        
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
            yield event.plain_result("当前没有被冷暴力的用户")
            return
        
        result = "当前冷暴力列表：\n"
        for record in records:
            remaining = self.cold_violence_mgr.format_remaining_time(record.remaining_time)
            result += f"- {record.user_name}({record.user_id})：剩余 {remaining}\n"
        
        yield event.plain_result(result.strip())
    
    @filter.llm_tool(name="cold_violence_user")
    async def cold_violence_tool(self, event: AstrMessageEvent, user_id: str, user_name: str, duration: int = 10) -> MessageEventResult:
        '''对指定用户实施冷暴力。

        Args:
            user_id(string): 用户ID
            user_name(string): 用户名称
            duration(number): 冷暴力时长（分钟），默认10分钟
        '''
        if not self.cold_violence_mgr.is_enabled():
            yield event.plain_result("冷暴力功能未启用")
            return
        
        if self.cold_violence_mgr.is_whitelisted(user_id):
            yield event.plain_result(f"用户 {user_name} 在白名单中，无法冷暴力")
            return
        
        if self.cold_violence_mgr.add_cold_violence(user_id, user_name, duration):
            yield event.plain_result(f"已对 {user_name} 实施冷暴力，时长 {duration} 分钟")
        else:
            yield event.plain_result(f"冷暴力失败")
    
    @filter.llm_tool(name="remove_cold_violence_user")
    async def remove_cold_violence_tool(self, event: AstrMessageEvent, user_id: str, user_name: str) -> MessageEventResult:
        '''解除指定用户的冷暴力。

        Args:
            user_id(string): 用户ID
            user_name(string): 用户名称
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
        '''检查指定用户是否被冷暴力。

        Args:
            user_id(string): 用户ID
            user_name(string): 用户名称
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
