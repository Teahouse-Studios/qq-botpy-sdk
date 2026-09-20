# -*- coding: utf-8 -*-
"""在 C2C/群聊 Markdown 消息中引用平台临时直链的示例。

``upload_media()`` 返回的 ``file_info`` 只能用于 ``msg_type=7`` 富媒体消息；
Markdown 图片需要 URL，因此这里使用 ``upload_media_url()`` 获取 ``raw_url``。
平台不一定对每个机器人开放 Markdown 权限，未开放时发送会返回权限错误。
"""
import os

import botpy
from botpy.protocol import MediaFileType, format_qqbot_markdown_image


class MyClient(botpy.Client):
    async def on_message(self, message):
        # 统一入站消息带有 reply_target，可直接用于上传与发送。
        target = message.reply_target
        with open(os.path.join(os.path.dirname(__file__), "resource", "test.png"), "rb") as source:
            data = source.read()

        # 小图片默认走一次性上传、只返回 file_info；需要直链时必须强制分片上传。
        uploaded = await self.upload_media_url(
            target,
            MediaFileType.IMAGE,
            data=data,
            file_name="test.png",
        )
        image_markdown = format_qqbot_markdown_image(uploaded.raw_url)

        # raw_url 是临时直链，uploaded.ttl 为剩余有效秒数，过期后需要重新上传。
        await self.send_markdown(target, content=f"收到消息：{message.content}\n{image_markdown}")


if __name__ == "__main__":
    intents = botpy.Intents.none()
    intents.public_messages = True

    client = MyClient(intents=intents)
    client.run(appid=os.environ["QQBOT_APP_ID"], secret=os.environ["QQBOT_APP_SECRET"])
