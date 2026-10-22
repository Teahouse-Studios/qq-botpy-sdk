# -*- coding: utf-8 -*-
"""使用素材管理接口把本地图片嵌进 Markdown 消息的示例。

素材图片（``/v2/media-images``）和富媒体消息的 ``file_info`` 是两套协议：
素材图片上传后得到 ``qqbot-media://image/v1/<token>`` 引用，只能用于 Markdown
内嵌图片，不占用文件配额，引用有效期滑动 90 天。

平台不一定对每个机器人开放 Markdown 权限，未开放时发送会返回权限错误。
"""
import os

import botpy


class MyClient(botpy.Client):
    async def on_message(self, message):
        target = message.reply_target
        local_path = os.path.join(os.path.dirname(__file__), "resource", "test.png")

        # 一步到位：上传素材并发送一条只含图片的 Markdown 消息。
        result = await self.send_markdown_image(target, local_path=local_path, alt="收到消息")
        print("素材引用：", result.url, "剩余有效秒数：", result.ttl)

        # 也可以分步使用：先上传拿引用，再自己拼 Markdown；image_id 可用于删除。
        uploaded = await self.upload_media_image(local_path=local_path, alt="收到消息")
        await self.send_markdown(target, f"收到消息：{message.content}\n{uploaded.markdown}")

        # 查看当前有效素材并清理旧图。
        page = await self.list_media_images()
        for item in page.get("images", []):
            if item.get("image_id") == uploaded.image_id:
                continue
            await self.delete_media_image(item["image_id"])


if __name__ == "__main__":
    intents = botpy.Intents.none()
    intents.public_messages = True

    client = MyClient(intents=intents)
    client.run(appid=os.environ["QQBOT_APP_ID"], secret=os.environ["QQBOT_APP_SECRET"])
