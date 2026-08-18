"""Agent 编排层。

这一层是唯一会调用大模型的地方，负责两件事：
1. 把用户的自然语言变成结构化的 :class:`UserPreference`（偏好解析）；
2. 给已经排好序的菜写推荐语（推荐语生成）。

两处调用都有规则降级路径，千帆不可用时系统降级但不瘫痪。
硬性过滤和打分排序在 `app/services/` 里，不属于这一层——
模型解析出的过敏原只是「输入」，不是「许可」，安全判断始终由规则层兜底。
"""

from app.agent.comment_writer import CommentWriter
from app.agent.orchestrator import RecommendAgent, get_agent
from app.agent.preference_parser import PreferenceParser, parse_by_keywords

__all__ = [
    "CommentWriter",
    "PreferenceParser",
    "RecommendAgent",
    "get_agent",
    "parse_by_keywords",
]
