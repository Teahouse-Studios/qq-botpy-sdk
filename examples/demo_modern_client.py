import os
import ssl

import botpy
from botpy.protocol import FileSessionStore, ReplyLimiter, UploadCache


class ModernClient(botpy.Client):
    async def on_message(self, message):
        # 长文本会自动切分；超过被动回复限制后会自动转主动消息。
        await self.send_text(message.reply_target, f"收到：{message.content}")

    async def on_interaction_context(self, context):
        interaction_id = context.event.data.get("id")
        if interaction_id:
            await self.acknowledge_interaction(interaction_id)


intents = botpy.Intents.none()
intents.public_messages = True
intents.interaction = True

client = ModernClient(
    intents=intents,
    markdown_support=os.getenv("QQBOT_MARKDOWN") == "1",
    user_agent="botpy-modern-example/1.0",
    ssl=ssl.create_default_context(),
    session_store=FileSessionStore("./.botpy-sessions"),
    upload_cache=UploadCache(),
    reply_limiter=ReplyLimiter(),
)

client.run(appid=os.environ["QQBOT_APP_ID"], secret=os.environ["QQBOT_APP_SECRET"])
