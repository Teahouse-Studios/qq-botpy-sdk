"""HTTP 代理配置的解析、校验与安全描述。

框架的 REST API、access token 和 Gateway WebSocket 都基于 httpx，因此统一把用户配置
归一化为 :class:`httpx.Proxy`。代理地址允许携带用户名和密码，日志中只会保留用户名。
"""

import importlib.util
from typing import Optional, Union

import httpx

#: 框架允许的代理协议；``socks5``/``socks5h`` 需要额外安装 ``socksio``。
PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
SOCKS_PROXY_SCHEMES = ("socks5", "socks5h")

#: ``proxy`` 参数接受的类型。
ProxyConfig = Union[str, httpx.Proxy, httpx.URL]


def normalize_proxy(value: Optional[ProxyConfig]) -> Optional[httpx.Proxy]:
    """把 ``proxy`` 配置归一化为 :class:`httpx.Proxy`。

    ``None`` 和空白字符串表示不使用代理；字符串必须是带主机名的
    ``http``/``https``/``socks5``/``socks5h`` URL；也可以直接传入已经构造好的
    ``httpx.Proxy`` 或 ``httpx.URL``。校验失败会抛出 :class:`ValueError`，
    类型不支持则抛出 :class:`TypeError`。
    """

    if value is None:
        return None
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        if _has_invalid_characters(candidate):
            raise ValueError("proxy URL must not contain control characters or whitespace")
        try:
            proxy = httpx.Proxy(candidate)
        except (ValueError, httpx.InvalidURL) as exc:
            raise ValueError(f"invalid proxy URL: {value!r} ({exc})") from exc
    elif isinstance(value, httpx.Proxy):
        # 已经是 httpx.Proxy 时保留其 auth/headers/ssl_context 配置。
        proxy = value
    elif isinstance(value, httpx.URL):
        try:
            proxy = httpx.Proxy(value)
        except (ValueError, httpx.InvalidURL) as exc:
            raise ValueError(f"invalid proxy URL: {value!r} ({exc})") from exc
    else:
        raise TypeError("proxy must be a str, httpx.Proxy, httpx.URL, or None")

    _validate_proxy(proxy)
    return proxy


def describe_proxy(proxy: Optional[ProxyConfig]) -> str:
    """返回可以安全写入日志的代理描述，隐藏密码和查询串。"""

    if proxy is None:
        return "<none>"
    if not isinstance(proxy, httpx.Proxy):
        try:
            proxy = normalize_proxy(proxy)
        except (TypeError, ValueError):
            return "<invalid proxy>"
        if proxy is None:
            return "<none>"
    url = proxy.url
    host = url.host or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = url.port
    except (ValueError, httpx.InvalidURL):
        port = None
    netloc = f"{host}:{port}" if port is not None else host
    credentials = f"{proxy.auth[0]}:***@" if proxy.auth else ""
    return f"{url.scheme}://{credentials}{netloc}"


def _validate_proxy(proxy: httpx.Proxy) -> None:
    url = proxy.url
    if url.scheme not in PROXY_SCHEMES:
        raise ValueError(f"unsupported proxy scheme {url.scheme!r}; expected one of {', '.join(PROXY_SCHEMES)}")
    if not url.host:
        raise ValueError("proxy URL must include a host")
    try:
        port = url.port
    except (ValueError, httpx.InvalidURL) as exc:
        raise ValueError(f"invalid proxy port in {describe_proxy(proxy)!r}") from exc
    if port is not None and not 0 < port < 65536:
        raise ValueError(f"proxy port {port} is out of range")
    if url.scheme in SOCKS_PROXY_SCHEMES and importlib.util.find_spec("socksio") is None:
        raise ValueError("socks5 proxy requires the 'socksio' package: pip install 'httpx[socks]'")


def _has_invalid_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 or character.isspace() for character in value)


__all__ = (
    "PROXY_SCHEMES",
    "ProxyConfig",
    "describe_proxy",
    "normalize_proxy",
)
