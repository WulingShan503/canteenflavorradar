---
name: canteen-radar-dev
description: 继续开发食堂味蕾雷达（食堂选餐 Agent 系统）。需要新增打分器、千帆客户端、Agent 编排、FastAPI 接口或前端页面，或需要了解本项目分层约定与验证方式时使用。
---

# 食堂味蕾雷达 开发指引

基于百度智能云千帆平台的大模型 Agent 食堂选餐系统，参赛作品。
仓库 `D:\canteenflavorradar`，远程 `github.com/WulingShan503/canteenflavorradar`。

## 开始前必做

1. 读 `README.md` 的「开发进度」清单，确认当前停在哪一步。
2. 读 `backend/app/models/enums.py`，所有新代码必须复用这里的枚举，不要自己造词。
3. 确认本步范围只覆盖清单里的一到两项——用户明确要求小步交付。

## 分层约定（不要破坏）

| 层 | 位置 | 由谁负责 |
| --- | --- | --- |
| 偏好解析 | `app/agent/` | 大模型，失败降级到关键词规则 |
| 硬性过滤 | `app/services/dish_repository.py` | 纯规则 |
| 排序打分 | `app/services/scorer.py` | 纯规则，输出 ScoreBreakdown |
| 推荐语生成 | `app/agent/` | 大模型，失败用规则理由兜底 |
| HTTP 接口 | `app/api/` | FastAPI 路由，薄一层 |

铁律：

- **过敏原、饮食限制（素食/清真等）永远由规则层拦截，绝不写成 prompt 约束。**
  `DishRepository.RELAX_STEPS` 逐级放宽列表里不得加入 `avoid_allergens` 或 `dietary_tags`。
- 大模型调用一律要有降级路径，千帆不可用时系统降级但不能瘫痪，
  响应里用 `RecommendResponse.fallback_used` 标记。
- 打分要保留各维度明细，不能只给总分——推荐理由要有据可依，调参也需要看是哪一维起作用。
- 打分层只排序候选集，**不许把被过滤掉的菜捞回来**，也不许在这层做安全判断。
- 权重之和必须为 100（`ScoreWeights.total()`），综合分才能落在 0-100；
  新增权重方案时先跑 `TestWeights::test_all_weight_sets_sum_to_100`。
- 用户没提的维度给中性分 `NEUTRAL`（0.5），不做惩罚；不能因为「没说预算」就扣分。
- 排序必须稳定可复现：并列时按销量、评分、菜品 id 逐级兜底。
- 千帆的 API Key 只从环境变量读，不写进代码或提交进仓库。

## 验证方式（重要）

本机**没有可用的 Python**，`pytest` 跑不了。写完带判断逻辑的代码后：

1. 在 `backend/` 下建临时 `_verify_xxx.js`，直接读 `app/data/*.json`，
   用 Node 等价复刻这段 Python 逻辑，逐条打印 PASS/FAIL。
2. 跑 `node _verify_xxx.js` 确认全通过。
3. **立刻 `rm` 掉临时脚本**，不要提交。
4. 同时把对应的 `pytest` 用例写进 `backend/tests/`，交给用户装好环境后自己跑。
5. 向用户汇报时说清：哪些是我实跑验证过的，哪些需要他 `pytest` 确认。

改动 `app/data/*.json` 后也用 Node 校验一遍字段完整性和枚举取值合法性。

## 收尾流程

每步做完，按顺序：

1. 本地 `git commit`（身份已在仓库本地配好，中文提交信息，正文列要点）。
   **默认不 push**，远程推送等用户明确要求。
2. 更新 `README.md` 的开发进度清单和目录结构。
3. 更新记忆 `canteen-flavor-radar-architecture.md` 里的进度段落。
4. 同步本文件的进度与新增约定。
5. 向用户汇报本步成果，停下等下一步指示，不要自动往下做。

## 进度

- [x] 骨架、enums 共享词表、Dish/UserPreference/Recommendation 模型
- [x] 示例数据 3 食堂 30 道菜（含过敏原、停供、各辣度边界）
- [x] `DishRepository`：硬性过滤 + 逐级放宽 + pytest 用例
- [x] `DishScorer`：六维加权打分、三套场景权重、`rank_diverse` 多样性约束、
      规则版 `reasons`；`build_reasons` 的输出后续要作为事实依据喂进推荐语 prompt
- [ ] **下一步：千帆客户端 `app/services/qianfan_client.py`**
      httpx 异步调用，access_token 缓存与刷新，超时/重试/熔断，
      失败一律抛自定义异常交由上层降级——客户端本身不做业务兜底。
      密钥走环境变量（`QIANFAN_AK` / `QIANFAN_SK`），不许进仓库，
      同步补一份 `.env.example`
- [ ] Agent 编排 `app/agent/`：偏好解析 + 推荐语生成 + 凑整餐 MealPlan
- [ ] FastAPI 接口 `app/api/`：推荐、菜品查询、食堂列表
- [ ] 前端页面

## 数据字段速查

菜品字段含义见 `app/models/dish.py`。示例数据里几个用于测边界的点：
`D1025 芝士焗饭` 是唯一 `available: false` 的菜；`D1017 酸辣粉` 含花生；
`D1011 兰州牛肉面` 和 `D1017` 含香菜（测忌口）；`D1021 香煎鸡胸沙拉` 是高蛋白低脂代表；
辣度分布 0 分 19 道、5 分 1 道（`D1007 水煮肉片`）。
