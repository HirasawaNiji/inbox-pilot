"""Privacy-conscious Markdown rendering for one local daily digest."""

from __future__ import annotations

from pathlib import Path

from inbox_agent.notifications.models import DailyDigest, DigestItem


def _single_line(value: str, *, limit: int) -> str:
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else f"{normalized[: limit - 1]}…"


def _item_line(item: DigestItem) -> str:
    subject = _single_line(item.subject or "（无主题）", limit=120)
    summary = _single_line(item.summary, limit=220)
    received = item.received_at.strftime("%Y-%m-%d %H:%M")
    return f"- **{item.priority.value}** · {subject} · {received}\n  - {summary}"


def render_daily_digest(digest: DailyDigest) -> str:
    """Render analyzed summaries and deadlines, never complete message bodies."""

    local_date = digest.generated_at.strftime("%Y-%m-%d")
    generated = digest.generated_at.strftime("%Y-%m-%d %H:%M %Z")
    lines = [
        f"# InboxPilot 每日摘要 · {local_date}",
        "",
        f"生成时间：{generated}",
        "",
        "## 优先事项",
        "",
    ]
    if digest.priority_items:
        for item in digest.priority_items:
            lines.append(_item_line(item))
    else:
        lines.append("- 最近没有新的 P1～P3 邮件。")
    lines.extend(["", "## 即将到期", ""])
    if digest.deadline_items:
        for item in digest.deadline_items:
            assert item.deadline is not None
            subject = _single_line(item.subject or "（无主题）", limit=120)
            deadline = item.deadline.strftime("%Y-%m-%d %H:%M %Z")
            lines.append(f"- **{deadline}** · {item.priority.value} · {subject}")
    else:
        lines.append("- 当前提醒窗口内没有可靠截止事项。")
    lines.extend(
        [
            "",
            "## 待处理",
            "",
            f"- 需要人工复核的邮件：**{digest.review_count}**",
            f"- 等待批准或拒绝的动作：**{digest.pending_action_count}**",
            f"- 已批准、等待执行的动作：**{digest.approved_action_count}**",
            "",
            (
                "> 隐私说明：本文件位于本地私有目录，只包含邮件主题和 Agent 摘要，"
                "不包含完整邮件正文、Token 或 API Key。"
            ),
            "",
        ]
    )
    return "\n".join(lines)


def write_daily_digest(digest: DailyDigest, output_dir: Path) -> Path:
    """Atomically replace the current local-date digest file."""

    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{digest.generated_at.date().isoformat()}.md"
    temporary = output_dir / f".{target.name}.tmp"
    temporary.write_text(render_daily_digest(digest), encoding="utf-8")
    temporary.replace(target)
    return target
