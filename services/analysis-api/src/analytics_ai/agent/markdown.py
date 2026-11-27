"""Server-owned Markdown structure; model prose and analytics values stay data."""

import re
from collections.abc import Iterable


def text(value: object) -> str:
    """Escape Markdown/HTML syntax without changing the underlying content."""
    return re.sub(r"([\\`*_{}\[\]<>#!|~])", r"\\\1", str(value))


def cell(value: object) -> str:
    return text(value).replace("\r", " ").replace("\n", " ")


def evidence(ids: Iterable[str]) -> str:
    # IDs come from verified facts, but still treat them as untrusted text.
    return "根拠: " + ", ".join("`" + i.replace("`", "") + "`" for i in ids)


def prose(value: str) -> str:
    """Separate existing fact/hypothesis markers, never invent factual claims."""
    escaped = text(value)
    for marker in ("確認された事実", "改善仮説"):
        escaped = escaped.replace(f"【{marker}】", f"\n\n**{marker}**\n\n")
    return escaped.strip()


def bounded_document(content: str, limit: int = 16_000) -> str:
    if len(content) <= limit:
        return content
    note = (
        "\n\n> 表示上限のため回答の末尾を省略しました。"
        "取得済みの詳細はグラフ・根拠データで確認してください。"
    )
    available = limit - len(note)
    boundary = content.rfind("\n\n", 0, available)
    return content[: boundary if boundary > 0 else available].rstrip("\\") + note
