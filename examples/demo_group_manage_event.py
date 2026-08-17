# -*- coding: utf-8 -*-
import os

import botpy
from botpy import logging
from botpy.ext.cog_yaml import read
from botpy.manage import GroupJoinRequestEvent, GroupManageEvent, GroupMemberEvent

test_config = read(os.path.join(os.path.dirname(__file__), "config.yaml"))

_log = logging.get_logger()


class MyClient(botpy.Client):
    async def on_group_add_robot(self, event: GroupManageEvent):
        _log.info("机器人被添加到群聊：" + str(event))
        await self.api.post_group_message(
            group_openid=event.group_openid,
            msg_type=0,
            event_id=event.event_id,
            content="hello",
        )

    async def on_group_del_robot(self, event: GroupManageEvent):
        _log.info("机器人被移除群聊：" + str(event))

    async def on_group_msg_reject(self, event: GroupManageEvent):
        _log.info("群聊关闭机器人主动消息：" + str(event))

    async def on_group_msg_receive(self, event: GroupManageEvent):
        _log.info("群聊打开机器人主动消息：" + str(event))

    async def on_group_member_add(self, event: GroupMemberEvent):
        _log.info("群成员加入：" + str(event))

    async def on_group_member_remove(self, event: GroupMemberEvent):
        _log.info("群成员退出：" + str(event))

    async def on_group_join_request(self, event: GroupJoinRequestEvent):
        _log.info("收到入群申请：" + str(event))
        # 请按业务规则完成风控检查后再审批；以下仅演示事件对象的便捷方法。
        # await event.approve()


if __name__ == "__main__":
    # 通过预设置的类型，设置需要监听的事件通道
    # intents = botpy.Intents.none()
    # intents.public_messages = True
    # intents.group_member_event = True

    # 通过kwargs，设置需要监听的事件通道
    intents = botpy.Intents(public_messages=True, group_member_event=True)
    client = MyClient(intents=intents)
    client.run(appid=test_config["appid"], secret=test_config["secret"])
