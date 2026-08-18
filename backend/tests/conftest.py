"""pytest 公共 fixture。"""

import sys
from pathlib import Path

import pytest

# 让 tests 目录下能直接 import app.*，无需安装成包
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.dish_repository import DishRepository  # noqa: E402


@pytest.fixture(scope="session")
def repo() -> DishRepository:
    """加载真实示例数据的仓库，顺带验证 30 条数据都能通过 Pydantic 校验。"""
    return DishRepository.from_json()
