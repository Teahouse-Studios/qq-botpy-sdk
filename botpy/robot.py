from .logging import get_logger
from botpy.types import robot
from .protocol.auth import TokenManager

_log = get_logger()


class Robot:
    def __init__(self, data: robot.Robot):
        self._update(data)

    def _update(self, data: robot.Robot) -> None:
        self.name = data.get("username")
        self.id = int(data["id"])
        self.avatar = data.get("avatar")


class Token:
    TYPE_BOT = "QQBot"
    TYPE_NORMAL = "Bearer"

    def __init__(
        self,
        app_id: str,
        secret: str,
        *,
        base_url: str = "https://bots.qq.com",
        timeout: float = 20,
        user_agent: str = "qq-botpy",
        ssl=None,
    ):
        """
        :param app_id:
            机器人appid
        :param secret:
            机器人密钥
        """
        self.app_id = app_id
        self.secret = secret
        self.Type = self.TYPE_BOT
        self._manager = TokenManager(
            app_id,
            secret,
            base_url=base_url,
            timeout=timeout,
            user_agent=user_agent,
            ssl=ssl,
            logger=_log,
        )

    @property
    def access_token(self):
        return self._manager.cached_token

    @access_token.setter
    def access_token(self, value):
        if value is None:
            self._manager.clear()
        else:
            self._manager.set_cached_token(value, self._manager.expires_at)

    @property
    def expires_in(self):
        return int(self._manager.expires_at)

    @expires_in.setter
    def expires_in(self, value):
        self._manager.set_cached_token(self.access_token, float(value))

    async def check_token(self):
        await self._manager.get_access_token()

    async def update_access_token(self):
        await self._manager.get_access_token(force_refresh=True)

    async def get_access_token(self, force_refresh: bool = False) -> str:
        return await self._manager.get_access_token(force_refresh=force_refresh)

    async def close(self):
        await self._manager.close()

    def start_background_refresh(self):
        return self._manager.start_background_refresh()

    async def stop_background_refresh(self):
        await self._manager.stop_background_refresh()

    # BotToken 机器人身份的 token
    def bot_token(self):
        return self

    # GetString 获取授权头字符串
    def get_string(self):
        if self.Type == self.TYPE_NORMAL:
            return self.access_token
        return "{} {}".format(self.Type, self.access_token)

    def get_type(self):
        return self.Type
