/* 页面逻辑。
 *
 * 只做三件事：收集表单条件、调 Api、把响应渲染成卡片。
 * 业务判断一律不在这里做——过滤和打分都在后端（或演示引擎）里，
 * 前端拿到的 recommendations 已经是排好序的最终结果。
 */

(function () {
  "use strict";

  // ---------- 常量 ----------

  const FLAVORS = ["辣", "麻", "酸", "甜", "咸", "鲜", "清淡", "蒜香", "酱香"];
  const DIETARY = ["素食", "纯素", "清真", "低脂", "低碳水", "高蛋白", "低糖"];
  const ALLERGENS = ["花生", "坚果", "海鲜", "贝类", "蛋类", "乳制品", "大豆", "麸质"];

  const DIM_LABELS = {
    flavor: "口味",
    cuisine: "菜系",
    price: "预算",
    nutrition: "营养",
    popularity: "口碑",
    convenience: "便利",
  };

  // 和 CSS 变量对应，用于得分条着色
  const DIM_COLORS = {
    flavor: "var(--dim-flavor)",
    cuisine: "var(--dim-cuisine)",
    price: "var(--dim-price)",
    nutrition: "var(--dim-nutrition)",
    popularity: "var(--dim-popularity)",
    convenience: "var(--dim-convenience)",
  };

  // 权重上限，用于把得分换算成条形长度（占该维满分的百分比）
  const WEIGHT_SETS = {
    default: { flavor: 30, cuisine: 14, price: 16, nutrition: 12, popularity: 18, convenience: 10 },
    nutritionFirst: { flavor: 22, cuisine: 10, price: 14, nutrition: 28, popularity: 16, convenience: 10 },
    popularFallback: { flavor: 0, cuisine: 0, price: 10, nutrition: 0, popularity: 62, convenience: 28 },
  };

  // 偏好字段的中文名，用于解析回显
  const PREF_LABELS = {
    liked_flavors: "想吃的口味",
    disliked_flavors: "不想要的口味",
    spicy_tolerance: "辣度上限",
    liked_cuisines: "菜系",
    categories: "品类",
    budget_min: "最低预算",
    budget_max: "预算上限",
    dietary_tags: "饮食要求",
    avoid_allergens: "规避过敏原",
    disliked_ingredients: "忌口",
    calorie_limit: "热量上限",
    min_protein: "蛋白质下限",
    meal_period: "餐段",
    preferred_canteens: "食堂",
    max_wait_minutes: "最长排队",
  };

  // 带单位的字段
  const PREF_UNITS = {
    budget_min: " 元",
    budget_max: " 元",
    calorie_limit: " 千卡",
    min_protein: " 克",
    max_wait_minutes: " 分钟",
    spicy_tolerance: " 级",
  };

  // 安全相关字段在回显里用醒目样式，让用户确认系统真的收到了
  const SAFETY_FIELDS = ["avoid_allergens", "dietary_tags", "disliked_ingredients"];

  // ---------- DOM 缓存 ----------

  const el = {};

  function cache() {
    const ids = [
      "recommend-form", "text-input", "submit-btn", "reset-btn",
      "flavor-checks", "dietary-checks", "allergen-checks",
      "budget-max", "spicy-tolerance", "canteen-select", "meal-period",
      "max-wait", "limit-select", "dislike-input", "meal-plan-toggle",
      "notice-area", "results", "results-meta", "card-list",
      "parsed-box", "parsed-tags",
      "meal-plan-box", "meal-plan-summary", "meal-plan-list",
      "mode-badge", "mode-text", "about-note", "advanced",
    ];
    ids.forEach((id) => {
      el[toCamel(id)] = document.getElementById(id);
    });
  }

  function toCamel(id) {
    return id.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
  }

  // ---------- 小工具 ----------

  /** 数字去掉多余小数，对应后端的 :g 格式 */
  function g(n) {
    if (typeof n !== "number" || Number.isNaN(n)) return String(n);
    return Number.isInteger(n) ? String(n) : String(parseFloat(n.toFixed(2)));
  }

  function elt(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function spicyText(level) {
    return ["不辣", "微微辣", "微辣", "中辣", "重辣", "特辣"][level] || `辣度 ${level}`;
  }

  // ---------- 初始化 ----------

  function buildChecks(container, values, extraClass) {
    values.forEach((value) => {
      const label = elt("label", "check" + (extraClass ? " " + extraClass : ""));
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = value;
      label.appendChild(input);
      label.appendChild(elt("span", null, value));
      container.appendChild(label);
    });
  }

  function bindExamples() {
    document.querySelectorAll(".chip[data-example]").forEach((chip) => {
      chip.addEventListener("click", () => {
        el.textInput.value = chip.dataset.example;
        el.textInput.focus();
        submit();
      });
    });
  }

  async function fillCanteens() {
    try {
      const data = await window.Api.listCanteens();
      (data.canteens || []).forEach((c) => {
        const option = document.createElement("option");
        option.value = c.name;
        option.textContent = c.location ? `${c.name}（${c.location}）` : c.name;
        el.canteenSelect.appendChild(option);
      });
    } catch (err) {
      // 食堂列表拿不到不影响主流程，下拉里就只剩「不限」
    }
  }

  function setMode(mode, health) {
    const badge = el.modeBadge;
    badge.classList.remove("is-live", "is-demo");

    if (mode === "live") {
      badge.classList.add("is-live");
      const full = health && health.mode === "full";
      el.modeText.textContent = full ? "已连接后端 · 大模型可用" : "已连接后端 · 纯规则模式";
      el.aboutNote.textContent = full
        ? "当前已连接后端且千帆可用，偏好解析与推荐语由大模型生成。"
        : "当前已连接后端，但未配置千帆密钥，偏好解析与推荐语走规则降级路径。"
          + "在 backend/.env 里填入 QIANFAN_AK / QIANFAN_SK 即可启用大模型。";
    } else {
      badge.classList.add("is-demo");
      el.modeText.textContent = "演示模式 · 纯前端运行";
      el.aboutNote.textContent =
        "当前没有检测到后端服务，页面正在用内置的演示引擎运行——"
        + "它是后端规则层（硬性过滤 + 六维打分）的等价 JS 移植，"
        + "所以过滤和打分结果与后端一致，只是推荐语走规则兜底而非大模型生成。"
        + "想看完整效果：在 backend 目录执行 uvicorn app.main:app --reload 后刷新本页。";
    }
  }

  // ---------- 收集条件 ----------

  function checkedValues(container) {
    return Array.from(container.querySelectorAll("input:checked")).map((i) => i.value);
  }

  function numberOrNull(input) {
    const raw = input.value.trim();
    if (!raw) return null;
    const value = Number(raw);
    return Number.isFinite(value) ? value : null;
  }

  /**
   * 把表单里勾的条件收成 preference 对象。
   * 只放用户明确给了的字段——空数组和 null 都不要传，
   * 后端把「没提」当中性处理，传空值反而可能被当成有效条件。
   */
  function collectPreference() {
    const pref = {};

    const flavors = checkedValues(el.flavorChecks);
    if (flavors.length) pref.liked_flavors = flavors;

    const dietary = checkedValues(el.dietaryChecks);
    if (dietary.length) pref.dietary_tags = dietary;

    const allergens = checkedValues(el.allergenChecks);
    if (allergens.length) pref.avoid_allergens = allergens;

    const budgetMax = numberOrNull(el.budgetMax);
    if (budgetMax !== null && budgetMax > 0) pref.budget_max = budgetMax;

    if (el.spicyTolerance.value !== "") {
      pref.spicy_tolerance = Number(el.spicyTolerance.value);
    }

    if (el.canteenSelect.value) pref.preferred_canteens = [el.canteenSelect.value];
    if (el.mealPeriod.value) pref.meal_period = el.mealPeriod.value;

    const maxWait = numberOrNull(el.maxWait);
    if (maxWait !== null && maxWait >= 0) pref.max_wait_minutes = Math.round(maxWait);

    const dislikes = el.dislikeInput.value
      .split(/[,，、\s]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (dislikes.length) pref.disliked_ingredients = dislikes;

    return Object.keys(pref).length ? pref : null;
  }

  // ---------- 提示条 ----------

  function showNotice(kind, icon, text) {
    const box = elt("div", "notice notice-" + kind);
    box.appendChild(elt("span", "notice-icon", icon));
    box.appendChild(elt("span", null, text));
    el.noticeArea.appendChild(box);
  }

  function renderNotices(data, error) {
    clear(el.noticeArea);

    if (error) {
      showNotice("error", "⚠", error);
      return;
    }
    if (!data) return;

    // 放宽说明和降级说明都在 message 里，直接展示
    if (data.message) {
      showNotice("info", "ℹ", data.message);
    }
    // fallback_used 单独提一句，但 message 里已经说了就不重复。
    // 没输入文字时不提「解析不可用」——本来就没有东西要解析，
    // 这时的降级只影响推荐语，说了反而让人以为出了故障。
    if (data.fallback_used && !data.message && el.textInput.value.trim()) {
      showNotice("warn", "⚠", "智能解析暂时不可用，本次结果按关键词规则给出，可能不够精准。");
    }
  }

  // ---------- 解析回显 ----------

  function formatPrefValue(key, value) {
    if (Array.isArray(value)) return value.join("、");
    if (typeof value === "boolean") return value ? "是" : "否";
    const unit = PREF_UNITS[key] || "";
    if (key === "spicy_tolerance") return spicyText(value);
    return g(value) + unit;
  }

  function renderParsed(pref) {
    clear(el.parsedTags);
    const entries = Object.entries(pref || {}).filter(([key, value]) => {
      if (!PREF_LABELS[key]) return false;
      if (value === null || value === undefined || value === "") return false;
      if (Array.isArray(value) && !value.length) return false;
      return true;
    });

    if (!entries.length) {
      el.parsedBox.hidden = true;
      return;
    }

    entries.forEach(([key, value]) => {
      const isSafety = SAFETY_FIELDS.includes(key);
      const tag = elt("span", "ptag" + (isSafety ? " is-safety" : ""));
      tag.appendChild(document.createTextNode(PREF_LABELS[key] + "："));
      tag.appendChild(elt("b", null, formatPrefValue(key, value)));
      el.parsedTags.appendChild(tag);
    });
    el.parsedBox.hidden = false;
  }

  // ---------- 卡片 ----------

  /** 从 breakdown 反推用的是哪套权重，好把得分换算成百分比。 */
  function guessWeights(breakdown) {
    // 热门兜底那套的口味/菜系/营养恒为 0，最容易辨认
    if (breakdown.flavor === 0 && breakdown.cuisine === 0 && breakdown.nutrition === 0) {
      return WEIGHT_SETS.popularFallback;
    }
    // 其余两套按各维上限判断：营养优先那套营养权重最高
    for (const name of ["default", "nutritionFirst"]) {
      const w = WEIGHT_SETS[name];
      const fits = Object.keys(DIM_LABELS).every(
        (k) => breakdown[k] <= w[k] + 0.01
      );
      if (fits) return w;
    }
    return WEIGHT_SETS.default;
  }

  function buildScoreRing(score) {
    const ring = elt("div", "score-ring");
    ring.style.setProperty("--pct", Math.max(0, Math.min(100, score)));
    ring.setAttribute("role", "img");
    ring.setAttribute("aria-label", `综合得分 ${g(score)} 分，满分 100`);
    ring.appendChild(elt("span", null, Math.round(score)));
    ring.appendChild(elt("small", null, "分"));
    return ring;
  }

  function buildBreakdown(breakdown) {
    const box = elt("div", "breakdown");

    const head = elt("div", "breakdown-head");
    head.appendChild(elt("span", null, "得分构成"));
    head.appendChild(elt("span", null, "条形长度 = 占该维满分的比例"));
    box.appendChild(head);

    const weights = guessWeights(breakdown);
    const bars = elt("div", "bars");

    Object.keys(DIM_LABELS).forEach((key) => {
      const value = breakdown[key] || 0;
      const max = weights[key] || 0;
      // 该维权重为 0 说明这套场景不考虑它，画出来只会让人困惑
      if (max === 0) return;

      const pct = Math.max(0, Math.min(100, (value / max) * 100));

      const row = elt("div", "bar-row");
      row.appendChild(elt("span", "bar-label", DIM_LABELS[key]));

      const track = elt("div", "bar-track");
      track.setAttribute("role", "img");
      track.setAttribute(
        "aria-label",
        `${DIM_LABELS[key]}：${g(value)} 分，该维满分 ${g(max)} 分`
      );
      const fill = elt("div", "bar-fill");
      fill.style.width = pct.toFixed(1) + "%";
      fill.style.background = DIM_COLORS[key];
      track.appendChild(fill);
      row.appendChild(track);

      const valueBox = elt("span", "bar-value");
      valueBox.appendChild(elt("b", null, g(value)));
      valueBox.appendChild(document.createTextNode("/" + g(max)));
      row.appendChild(valueBox);

      bars.appendChild(row);
    });

    box.appendChild(bars);
    return box;
  }

  function buildCard(rec, index, fallbackUsed) {
    const dish = rec.dish;
    const card = elt("article", "card" + (index === 0 ? " is-top" : ""));

    // --- 头部：名称 + 得分环 ---
    const head = elt("div", "card-head");
    const title = elt("div", "card-title");

    const rankRow = elt("div", "rank-row");
    rankRow.appendChild(elt("span", "rank", "#" + (index + 1)));
    rankRow.appendChild(elt("span", "dish-name", dish.name));
    title.appendChild(rankRow);
    title.appendChild(elt("span", "dish-where", `${dish.canteen} · ${dish.window}`));

    head.appendChild(title);
    head.appendChild(buildScoreRing(rec.score));
    card.appendChild(head);

    // --- 标签行 ---
    const meta = elt("div", "card-meta");
    meta.appendChild(elt("span", "tag tag-price", g(dish.price) + " 元"));
    meta.appendChild(elt("span", "tag", dish.cuisine));
    meta.appendChild(elt("span", "tag", dish.category));
    if (dish.spicy_level > 0) {
      meta.appendChild(elt("span", "tag tag-spicy", spicyText(dish.spicy_level)));
    }
    (dish.dietary_tags || []).forEach((t) => {
      meta.appendChild(elt("span", "tag tag-diet", t));
    });
    if (dish.signature) {
      meta.appendChild(elt("span", "tag tag-signature", "★ 招牌"));
    }
    meta.appendChild(elt("span", "tag", `${g(dish.rating)} 分 / ${dish.rating_count} 评`));
    meta.appendChild(elt("span", "tag", `排队约 ${dish.wait_minutes} 分`));
    // 含过敏原要明确标出来——用户没勾规避，但知情权还是要给
    (dish.allergens || []).forEach((a) => {
      meta.appendChild(elt("span", "tag tag-allergen", "含" + a));
    });
    card.appendChild(meta);

    // --- 推荐语 ---
    if (rec.comment) {
      const comment = elt("div", "comment");
      comment.appendChild(document.createTextNode(rec.comment));
      comment.appendChild(
        elt("span", "comment-source", fallbackUsed ? "规则生成" : "大模型生成")
      );
      card.appendChild(comment);
    }

    // --- 理由 ---
    if (rec.reasons && rec.reasons.length) {
      const list = elt("ul", "reasons");
      rec.reasons.forEach((reason) => list.appendChild(elt("li", null, reason)));
      card.appendChild(list);
    }

    // --- 得分明细 ---
    if (rec.breakdown) card.appendChild(buildBreakdown(rec.breakdown));

    // --- 营养 ---
    const nut = dish.nutrition;
    if (nut) {
      const row = elt("div", "nutrition");
      [
        ["热量", g(nut.calories) + " 千卡"],
        ["蛋白", g(nut.protein) + " 克"],
        ["脂肪", g(nut.fat) + " 克"],
        ["碳水", g(nut.carbs) + " 克"],
      ].forEach(([label, value]) => {
        const item = elt("span");
        item.appendChild(document.createTextNode(label + " "));
        item.appendChild(elt("b", null, value));
        row.appendChild(item);
      });
      card.appendChild(row);
    }

    return card;
  }

  // ---------- 凑整餐 ----------

  function renderMealPlan(plan) {
    if (!plan || !plan.items || !plan.items.length) {
      el.mealPlanBox.hidden = true;
      return;
    }
    el.mealPlanSummary.textContent = plan.summary || "";
    clear(el.mealPlanList);

    plan.items.forEach((item) => {
      const li = elt("li");
      li.appendChild(elt("span", "mp-category", item.dish.category));
      li.appendChild(elt("span", "mp-name", item.dish.name));
      li.appendChild(elt("span", "mp-where", `${item.dish.canteen} · ${item.dish.window}`));
      li.appendChild(elt("span", "mp-price", g(item.dish.price) + " 元"));
      el.mealPlanList.appendChild(li);
    });
    el.mealPlanBox.hidden = false;
  }

  // ---------- 渲染入口 ----------

  function renderSkeletons(count) {
    clear(el.cardList);
    for (let i = 0; i < count; i += 1) {
      const card = elt("div", "skeleton-card");
      ["sk-line w-60", "sk-line w-40", "sk-line tall", "sk-line w-80", "sk-line w-60"]
        .forEach((cls) => card.appendChild(elt("div", cls)));
      el.cardList.appendChild(card);
    }
  }

  function renderEmpty(message) {
    clear(el.cardList);
    const box = elt("div", "empty-state");
    box.appendChild(elt("span", "empty-icon", "🍽️"));
    box.appendChild(elt("h3", null, "没找到合适的菜"));
    box.appendChild(
      elt("p", null, message || "试着放宽一点条件，比如提高预算上限或换个食堂。")
    );
    el.cardList.appendChild(box);
  }

  function render(data) {
    renderNotices(data, null);
    renderParsed(data.parsed_preference);

    const recs = data.recommendations || [];

    if (!recs.length) {
      el.resultsMeta.textContent = "";
      renderEmpty(data.message);
      el.mealPlanBox.hidden = true;
      return;
    }

    el.resultsMeta.textContent =
      `从 ${data.total_candidates} 道可选菜里挑出 ${recs.length} 道` +
      (data.fallback_used ? "（规则模式）" : "（大模型参与）");

    clear(el.cardList);
    recs.forEach((rec, index) => {
      el.cardList.appendChild(buildCard(rec, index, data.fallback_used));
    });

    renderMealPlan(data.meal_plan);
  }

  // ---------- 提交 ----------

  let pending = false;

  async function submit() {
    if (pending) return;
    pending = true;

    el.submitBtn.disabled = true;
    el.submitBtn.querySelector(".btn-label").textContent = "正在挑…";

    const limit = Number(el.limitSelect.value) || 5;
    renderSkeletons(Math.min(limit, 3));

    const payload = {
      text: el.textInput.value.trim(),
      limit,
      with_meal_plan: el.mealPlanToggle.checked,
    };
    const pref = collectPreference();
    if (pref) payload.preference = pref;

    try {
      const data = await window.Api.recommend(payload);
      render(data);
    } catch (err) {
      renderNotices(null, err.message || "请求失败，请稍后重试。");
      renderEmpty("请求没成功，检查一下输入条件或稍后再试。");
    } finally {
      pending = false;
      el.submitBtn.disabled = false;
      el.submitBtn.querySelector(".btn-label").textContent = "帮我推荐";
    }
  }

  function reset() {
    el.recommendForm.reset();
    el.textInput.value = "";
    // reset 不会清掉动态生成的 checkbox 的 checked 属性在某些浏览器里的残留
    document
      .querySelectorAll(".checks input:checked")
      .forEach((input) => (input.checked = false));
    clear(el.noticeArea);
    el.parsedBox.hidden = true;
    el.mealPlanBox.hidden = true;
    el.resultsMeta.textContent = "";
    el.textInput.focus();
    submit();
  }

  // ---------- 启动 ----------

  async function init() {
    cache();

    buildChecks(el.flavorChecks, FLAVORS);
    buildChecks(el.dietaryChecks, DIETARY);
    buildChecks(el.allergenChecks, ALLERGENS, "is-allergen");
    bindExamples();

    el.recommendForm.addEventListener("submit", (event) => {
      event.preventDefault();
      submit();
    });
    el.resetBtn.addEventListener("click", reset);

    const state = await window.Api.probe();
    setMode(state.mode, state.health);
    await fillCanteens();

    // 首屏直接给热门推荐，别让用户看到一片空白不知道该干什么
    submit();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
