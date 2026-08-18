# 食堂味蕾雷达 (Canteen Flavor Radar)

基于 Agent 的高校食堂选餐系统。首届全国人工智能应用创新大赛校赛参赛作品。

针对高校食堂菜品信息不透明、学生选餐决策效率低等痛点，基于百度智能云千帆平台，
利用大模型驱动的 Agent 架构，实现从用户口味偏好输入到个性化菜品推荐的全流程智能化服务。

## 设计思路

核心是把「能不能吃」和「有多想吃」拆开：

| 层次 | 职责 | 特点 |
| --- | --- | --- |
| 偏好解析 | 自然语言 → 结构化偏好 | 大模型负责，失败时降级到关键词规则 |
| 硬性过滤 | 过敏原、忌口、饮食限制、预算上限、辣度上限 | 纯规则，结果确定可复现 |
| 排序打分 | 口味、菜系、预算、营养、口碑、便利度加权 | 纯规则，得分明细可解释 |
| 推荐语生成 | 结合菜品信息和用户原话写推荐理由 | 大模型负责，失败时用规则理由兜底 |

这样拆的原因：过敏原之类的安全约束不能交给可能产生幻觉的模型来把关，必须由确定性代码兜住；
而「为什么推荐这道菜」这类需要语感的表达，规则写出来又生硬，正好交给大模型。
模型不可用时系统降级但不瘫痪。

## 目录结构

```
backend/
  app/
    models/          数据模型（Pydantic）
      enums.py       共享词表：口味、菜系、品类、餐段、饮食标签、过敏原
      dish.py        菜品、食堂、营养成分
      preference.py  用户偏好
      recommendation.py  推荐结果与得分明细
    services/
      dish_repository.py  数据加载 + 硬性过滤 + 条件过严时逐级放宽
    data/
      dishes.json    示例菜品数据（3 个食堂 30 道菜）
      canteens.json  食堂基础信息
    agent/           Agent 编排（待建）
    api/             FastAPI 路由（待建）
  tests/
```

## 开发进度

- [x] 项目骨架、数据模型、示例数据
- [x] 数据仓库：硬性过滤与逐级放宽
- [ ] 打分排序器
- [ ] 千帆平台客户端
- [ ] Agent 编排：偏好解析 + 推荐语生成
- [ ] FastAPI 接口
- [ ] 前端页面

## 本地运行

需要 Python 3.11+。

```bash
cd backend
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
pytest
```

## 示例数据说明

`backend/app/data/dishes.json` 是按合理字段设计的模拟数据，覆盖了各种边界情况
（辣度 0–5、价格 1–22 元、素食/清真/低脂等标签、含过敏原的菜、以及一道停止供应的菜），
便于测试过滤逻辑。接入真实菜单时替换该文件即可，字段含义见 `app/models/dish.py`。
