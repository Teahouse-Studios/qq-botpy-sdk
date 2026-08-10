from typing import List


TEXT_CHUNK_LIMIT = 5000


def chunk_text(text: str, limit: int = TEXT_CHUNK_LIMIT) -> List[str]:
    """按字符窗口切分文本，保证每段不超过平台单消息限制。"""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    return [text[index : index + limit] for index in range(0, len(text), limit)] or [""]
