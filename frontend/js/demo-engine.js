/* 演示引擎：把后端规则层等价移植到浏览器里。
 *
 * 为什么要这份移植：后端是 Python，评审或同学打开这个页面时未必装了环境。
 * 有了它，页面用 file:// 双击打开就能看到完整的过滤 + 打分 + 推荐语效果；
 * 后端起着的时候 api.js 会自动切回真实接口，这份代码就不会被调用。
 *
 * 移植的对应关系（改后端规则时这里要跟着改）：
 *   parseByKeywords  ← app/agent/preference_parser.py + keyword_rules.py
 *   findCandidates   ← app/services/dish_repository.py
 *   rankDiverse      ← app/services/scorer.py
 *   fallbackComment  ← app/agent/comment_writer.py
 *
 * 注意：这里没有大模型，所以推荐语走的是规则兜底那条路，
 * 和后端 fallback_used=true 时的输出一致。
 */

(function (global) {
  "use strict";

  // ---------- 词表（对应 keyword_rules.py）----------

  const FLAVOR_WORDS = {
    辣: ["辣", "香辣", "麻辣", "重口"],
    麻: ["麻", "花椒"],
    酸: ["酸", "醋"],
    甜: ["甜", "糖"],
    咸: ["咸"],
    鲜: ["鲜", "鲜香"],
    清淡: ["清淡", "淡", "不油", "少油", "养胃", "口味轻"],
    油腻: ["油腻", "重油"],
    蒜香: ["蒜香", "蒜"],
    酱香: ["酱香", "酱"],
  };

  // 单字口味词容易被更长的词包住，「海鲜」里的「鲜」不是口味诉求
  const FLAVOR_EXCLUSIONS = {
    鲜: ["海鲜", "生鲜", "鲜奶", "新鲜"],
    糖: ["低糖", "控糖", "无糖", "糖尿"],
    淡: ["淡水"],
    酸: ["氨基酸", "酸奶"],
    蒜: ["蒜苗"],
    麻: ["芝麻", "麻酱", "麻烦"],
    酱: ["蒜香酱"],
  };

  const NEGATIONS = ["不", "别", "不要", "不想", "讨厌", "受不了", "怕", "少", "无"];

  const CUISINE_WORDS = {
    川菜: ["川菜", "四川", "川味"],
    湘菜: ["湘菜", "湖南"],
    粤菜: ["粤菜", "广东", "广式"],
    鲁菜: ["鲁菜", "山东"],
    淮扬菜: ["淮扬菜", "淮扬", "江浙"],
    东北菜: ["东北菜", "东北"],
    西北菜: ["西北菜", "西北", "兰州"],
    家常菜: ["家常菜", "家常"],
    西式: ["西餐", "西式", "意面", "汉堡"],
    日韩: ["日料", "日式", "韩式", "韩国", "寿司", "拌饭"],
    快餐: ["快餐", "速食"],
  };

  const CATEGORY_WORDS = {
    主食: ["主食", "米饭", "面食", "馒头"],
    荤菜: ["荤菜", "肉", "吃肉"],
    素菜: ["素菜", "青菜", "蔬菜"],
    汤品: ["汤", "汤品", "粥"],
    小吃: ["小吃", "零嘴"],
    饮品: ["饮品", "喝的", "豆浆", "饮料"],
    套餐: ["套餐", "盖饭", "简餐"],
  };

  const DIETARY_WORDS = {
    素食: ["素食", "吃素", "斋"],
    纯素: ["纯素", "全素"],
    清真: ["清真", "穆斯林"],
    低脂: ["低脂", "减脂", "减肥", "低油", "刷脂"],
    低碳水: ["低碳水", "低碳", "断碳"],
    高蛋白: ["高蛋白", "增肌", "练完", "健身", "补蛋白"],
    低糖: ["低糖", "控糖", "无糖"],
  };

  const ALLERGEN_WORDS = {
    花生: ["花生"],
    坚果: ["坚果", "核桃", "杏仁", "腰果"],
    海鲜: ["海鲜", "鱼", "虾"],
    贝类: ["贝类", "扇贝", "蛤"],
    蛋类: ["鸡蛋", "蛋类", "蛋"],
    乳制品: ["乳制品", "牛奶", "奶", "芝士", "乳糖"],
    大豆: ["大豆", "豆制品", "黄豆"],
    麸质: ["麸质", "面筋", "小麦"],
  };

  const ALLERGY_MARKERS = ["过敏", "忌", "不能吃", "吃不了", "不耐受", "禁忌"];

  const MEAL_PERIOD_WORDS = {
    早餐: ["早餐", "早饭", "早上"],
    午餐: ["午餐", "午饭", "中午"],
    晚餐: ["晚餐", "晚饭", "晚上"],
    夜宵: ["夜宵", "宵夜", "半夜"],
  };

  const COMMON_DISLIKES = [
    "香菜", "葱", "姜", "蒜", "内脏", "肥肉",
    "苦瓜", "胡萝卜", "洋葱", "芹菜", "羊肉", "辣椒",
  ];

  const SPICY_TOLERANCE_WORDS = [
    [["微辣", "小辣", "一点辣", "轻辣"], 2],
    [["中辣", "适中"], 3],
    [["特辣", "变态辣", "巨辣", "越辣越好", "重辣"], 5],
    [["不吃辣", "不能吃辣", "怕辣", "不要辣", "不辣", "忌辣"], 0],
  ];

  const CHEAP_WORDS = ["便宜", "省钱", "实惠", "划算", "穷", "预算紧", "便宜点"];
  const TREAT_WORDS = ["吃好点", "奖励自己", "改善", "不差钱", "贵点"];
  const CHEAP_BUDGET_MAX = 10;
  const TREAT_BUDGET_MIN = 15;

  // 金额正则必须带「块/元」，否则「排队不超过10分钟」会被当成预算 10 元
  const MONEY = "(?:块钱|块|元)";
  const BUDGET_MAX_PATTERNS = [
    new RegExp("(\\d+(?:\\.\\d+)?)\\s*" + MONEY + "\\s*(?:以内|以下|之内|左右|封顶)"),
    new RegExp("(?:不超过|不高于|最多|别超过|控制在)\\s*(\\d+(?:\\.\\d+)?)\\s*" + MONEY),
    new RegExp("预算\\s*(?:是|在)?\\s*(\\d+(?:\\.\\d+)?)\\s*" + MONEY),
  ];
  const BUDGET_MIN_PATTERNS = [
    new RegExp("(\\d+(?:\\.\\d+)?)\\s*" + MONEY + "\\s*(?:以上|往上|起步)"),
    new RegExp("(?:不低于|至少|起码)\\s*(\\d+(?:\\.\\d+)?)\\s*" + MONEY),
  ];
  const CALORIE_PATTERNS = [
    new RegExp("(\\d+(?:\\.\\d+)?)\\s*(?:千卡|大卡|卡|kcal)\\s*(?:以内|以下)?"),
  ];
  const PROTEIN_PATTERNS = [
    new RegExp("蛋白(?:质)?\\s*(?:至少|不低于)?\\s*(\\d+(?:\\.\\d+)?)\\s*(?:克|g)"),
  ];
  const WAIT_PATTERNS = [
    new RegExp("(?:排队|等)\\s*(?:不超过|少于|别超过)?\\s*(\\d+)\\s*(?:分钟|分)"),
    new RegExp("(\\d+)\\s*(?:分钟|分)\\s*(?:以内|之内|以下)"),
  ];
  const CANTEEN_PATTERN = /([一二三四五六七八九十\d]+)\s*食堂/g;
  const CLAUSE_SEPARATORS = /[，,。；;！!？?、\s]+/;

  // ---------- 词表匹配辅助 ----------

  function splitClauses(text) {
    return (text || "").split(CLAUSE_SEPARATORS).filter(Boolean);
  }

  /** 关键词是否作为独立诉求出现（不被排除词包住）。 */
  function wordHit(text, word) {
    const exclusions = FLAVOR_EXCLUSIONS[word];
    if (!exclusions) return text.includes(word);

    let idx = text.indexOf(word);
    while (idx !== -1) {
      let covered = false;
      for (const bad of exclusions) {
        let b = text.indexOf(bad);
        while (b !== -1) {
          if (b <= idx && idx < b + bad.length) {
            covered = true;
            break;
          }
          b = text.indexOf(bad, b + 1);
        }
        if (covered) break;
      }
      if (!covered) return true;
      idx = text.indexOf(word, idx + 1);
    }
    return false;
  }

  /** 关键词前一小段里有没有否定词。中文否定词都在被否定对象之前。 */
  function findNegation(text, keyword, window) {
    window = window || 4;
    let idx = text.indexOf(keyword);
    while (idx !== -1) {
      let segment = text.slice(Math.max(0, idx - window), idx);
      for (const sep of "，,。；;！!？?") {
        if (segment.includes(sep)) {
          const parts = segment.split(sep);
          segment = parts[parts.length - 1];
        }
      }
      if (NEGATIONS.some((n) => segment.includes(n))) return true;
      idx = text.indexOf(keyword, idx + 1);
    }
    return false;
  }

  function matchFirst(text, patterns) {
    for (const pattern of patterns) {
      const m = text.match(pattern);
      if (m) {
        const value = parseFloat(m[1]);
        if (!Number.isNaN(value)) return value;
      }
    }
    return null;
  }

  function collect(text, table) {
    const found = [];
    for (const key of Object.keys(table)) {
      for (const word of table[key]) {
        if (wordHit(text, word) && !findNegation(text, word)) {
          if (!found.includes(key)) found.push(key);
          break;
        }
      }
    }
    return found;
  }

  // ---------- 偏好解析（关键词规则）----------

  function parseByKeywords(rawText) {
    const text = (rawText || "").trim();
    const pref = {
      liked_flavors: [],
      disliked_flavors: [],
      liked_cuisines: [],
      categories: [],
      dietary_tags: [],
      avoid_allergens: [],
      disliked_ingredients: [],
      preferred_canteens: [],
      spicy_tolerance: null,
      budget_min: null,
      budget_max: null,
      calorie_limit: null,
      min_protein: null,
      max_wait_minutes: null,
      meal_period: null,
      raw_text: text,
    };
    if (!text) return pref;

    for (const flavor of Object.keys(FLAVOR_WORDS)) {
      for (const word of FLAVOR_WORDS[flavor]) {
        if (!wordHit(text, word)) continue;
        if (findNegation(text, word)) {
          if (!pref.disliked_flavors.includes(flavor)) pref.disliked_flavors.push(flavor);
        } else if (!pref.liked_flavors.includes(flavor)) {
          pref.liked_flavors.push(flavor);
        }
        break;
      }
    }
    // 同一口味既喜欢又讨厌时以「喜欢」为准
    pref.disliked_flavors = pref.disliked_flavors.filter(
      (f) => !pref.liked_flavors.includes(f)
    );

    pref.liked_cuisines = collect(text, CUISINE_WORDS);
    pref.categories = collect(text, CATEGORY_WORDS);
    pref.dietary_tags = collect(text, DIETARY_WORDS);

    // 过敏原要求标记词与过敏原在同一分句
    for (const clause of splitClauses(text)) {
      if (!ALLERGY_MARKERS.some((m) => clause.includes(m))) continue;
      for (const allergen of Object.keys(ALLERGEN_WORDS)) {
        if (
          ALLERGEN_WORDS[allergen].some((w) => clause.includes(w)) &&
          !pref.avoid_allergens.includes(allergen)
        ) {
          pref.avoid_allergens.push(allergen);
        }
      }
    }

    for (const item of COMMON_DISLIKES) {
      if (
        text.includes(item) &&
        findNegation(text, item) &&
        !pref.disliked_ingredients.includes(item)
      ) {
        pref.disliked_ingredients.push(item);
      }
    }

    for (const [words, level] of SPICY_TOLERANCE_WORDS) {
      if (words.some((w) => text.includes(w))) {
        pref.spicy_tolerance = level;
        break;
      }
    }

    let budgetMax = matchFirst(text, BUDGET_MAX_PATTERNS);
    if (budgetMax === null && CHEAP_WORDS.some((w) => text.includes(w))) {
      budgetMax = CHEAP_BUDGET_MAX;
    }
    if (budgetMax !== null) pref.budget_max = budgetMax;

    let budgetMin = matchFirst(text, BUDGET_MIN_PATTERNS);
    if (budgetMin === null && TREAT_WORDS.some((w) => text.includes(w))) {
      budgetMin = TREAT_BUDGET_MIN;
    }
    if (budgetMin !== null && (budgetMax === null || budgetMin <= budgetMax)) {
      pref.budget_min = budgetMin;
    }

    pref.calorie_limit = matchFirst(text, CALORIE_PATTERNS);
    pref.min_protein = matchFirst(text, PROTEIN_PATTERNS);
    const wait = matchFirst(text, WAIT_PATTERNS);
    if (wait !== null) pref.max_wait_minutes = Math.round(wait);

    for (const period of Object.keys(MEAL_PERIOD_WORDS)) {
      if (MEAL_PERIOD_WORDS[period].some((w) => text.includes(w))) {
        pref.meal_period = period;
        break;
      }
    }

    const canteens = [];
    let m;
    CANTEEN_PATTERN.lastIndex = 0;
    while ((m = CANTEEN_PATTERN.exec(text)) !== null) {
      const name = m[1] + "食堂";
      if (!canteens.includes(name)) canteens.push(name);
    }
    pref.preferred_canteens = canteens;

    return pref;
  }

  function isEmptyPreference(pref) {
    return !(
      pref.liked_flavors.length ||
      pref.disliked_flavors.length ||
      pref.spicy_tolerance !== null ||
      pref.liked_cuisines.length ||
      pref.categories.length ||
      pref.budget_min !== null ||
      pref.budget_max !== null ||
      pref.dietary_tags.length ||
      pref.avoid_allergens.length ||
      pref.disliked_ingredients.length ||
      pref.calorie_limit !== null ||
      pref.min_protein !== null ||
      pref.preferred_canteens.length ||
      pref.max_wait_minutes !== null
    );
  }

  // ---------- 硬性过滤（对应 dish_repository.py）----------

  // 逐级放宽顺序。过敏原和饮食限制不在此列，是安全底线，任何情况都不放宽。
  const RELAX_STEPS = [
    "max_wait_minutes",
    "categories",
    "preferred_canteens",
    "spicy_tolerance",
    "budget_max",
    "calorie_limit",
  ];

  const RELAX_LABELS = {
    max_wait_minutes: "放宽了排队时长要求",
    categories: "扩大了菜品品类范围",
    preferred_canteens: "扩大到了其他食堂",
    spicy_tolerance: "略微放宽了辣度上限",
    budget_max: "略微上调了预算上限",
    calorie_limit: "略微上调了热量上限",
  };

  function containsIngredient(dish, keywords) {
    for (const raw of keywords) {
      const kw = (raw || "").trim();
      if (!kw) continue;
      for (const ing of dish.ingredients) {
        if (ing.includes(kw) || kw.includes(ing)) return true;
      }
      if (dish.name.includes(kw)) return true;
    }
    return false;
  }

  function matches(dish, pref, relaxed) {
    if (!dish.available) return false;

    // --- 安全底线，不参与放宽 ---
    if (
      pref.avoid_allergens.length &&
      pref.avoid_allergens.some((a) => dish.allergens.includes(a))
    ) {
      return false;
    }
    if (
      pref.dietary_tags.length &&
      !pref.dietary_tags.every((t) => dish.dietary_tags.includes(t))
    ) {
      return false;
    }
    if (
      pref.disliked_ingredients.length &&
      containsIngredient(dish, pref.disliked_ingredients)
    ) {
      return false;
    }
    if (pref.meal_period && dish.meal_periods.length) {
      if (!dish.meal_periods.includes(pref.meal_period)) return false;
    }

    // --- 以下可逐级放宽 ---
    // budget_min 故意不在这里拦，交给打分层降权
    if (!relaxed.has("budget_max")) {
      if (pref.budget_max !== null && dish.price > pref.budget_max) return false;
    } else if (pref.budget_max !== null && dish.price > pref.budget_max * 1.2) {
      return false;
    }

    if (!relaxed.has("spicy_tolerance")) {
      if (pref.spicy_tolerance !== null && dish.spicy_level > pref.spicy_tolerance) {
        return false;
      }
    } else if (
      pref.spicy_tolerance !== null &&
      dish.spicy_level > pref.spicy_tolerance + 1
    ) {
      return false;
    }

    if (!relaxed.has("categories")) {
      if (pref.categories.length && !pref.categories.includes(dish.category)) return false;
    }

    if (!relaxed.has("preferred_canteens")) {
      if (
        pref.preferred_canteens.length &&
        !pref.preferred_canteens.includes(dish.canteen)
      ) {
        return false;
      }
    }

    if (!relaxed.has("max_wait_minutes")) {
      if (pref.max_wait_minutes !== null && dish.wait_minutes > pref.max_wait_minutes) {
        return false;
      }
    }

    if (!relaxed.has("calorie_limit")) {
      if (pref.calorie_limit !== null && dish.nutrition.calories > pref.calorie_limit) {
        return false;
      }
    } else if (
      pref.calorie_limit !== null &&
      dish.nutrition.calories > pref.calorie_limit * 1.15
    ) {
      return false;
    }

    return true;
  }

  function stepApplies(pref, step) {
    switch (step) {
      case "max_wait_minutes": return pref.max_wait_minutes !== null;
      case "categories": return pref.categories.length > 0;
      case "preferred_canteens": return pref.preferred_canteens.length > 0;
      case "spicy_tolerance": return pref.spicy_tolerance !== null;
      case "budget_max": return pref.budget_max !== null;
      case "calorie_limit": return pref.calorie_limit !== null;
      default: return false;
    }
  }

  function findCandidates(dishes, pref, minResults) {
    minResults = minResults || 5;
    let candidates = dishes.filter((d) => matches(d, pref, new Set()));
    if (candidates.length >= minResults) return { candidates, notes: [] };

    const relaxed = new Set();
    const notes = [];
    for (const step of RELAX_STEPS) {
      if (!stepApplies(pref, step)) continue;
      relaxed.add(step);
      const widened = dishes.filter((d) => matches(d, pref, relaxed));
      // 只有真的多筛出菜才算放宽过，否则会给出「已上调热量上限」
      // 但结果全在原上限内的怪提示
      if (widened.length > candidates.length) notes.push(RELAX_LABELS[step]);
      candidates = widened;
      if (candidates.length >= minResults) break;
    }
    return { candidates, notes };
  }

  // ---------- 打分排序（对应 scorer.py）----------

  const NEUTRAL = 0.5;
  const RATING_PRIOR_COUNT = 120;
  const RATING_PRIOR_SCORE = 4.0;
  const RATING_FLOOR = 3.0;
  const POPULARITY_FULL = 800;
  const WAIT_TOLERABLE = 20;
  const CROWD_MAX = 5;
  const PROTEIN_RATIO_FULL = 0.35;
  const FAT_RATIO_BAD = 0.45;
  const CARB_RATIO_BAD = 0.6;
  const CALORIE_SWEET_SPOT = 0.6;
  const CALORIE_DECAY_SPAN = 0.9;
  const HIGHLIGHT_RATIO = 0.7;
  const MAX_REASONS = 4;

  const DEFAULT_WEIGHTS = {
    flavor: 30, cuisine: 14, price: 16, nutrition: 12, popularity: 18, convenience: 10,
  };
  const NUTRITION_FIRST_WEIGHTS = {
    flavor: 22, cuisine: 10, price: 14, nutrition: 28, popularity: 16, convenience: 10,
  };
  const POPULAR_FALLBACK_WEIGHTS = {
    flavor: 0, cuisine: 0, price: 10, nutrition: 0, popularity: 62, convenience: 28,
  };
  const NUTRITION_TAGS = ["低脂", "低碳水", "高蛋白", "低糖"];

  const clamp = (v, lo, hi) => Math.max(lo === undefined ? 0 : lo, Math.min(hi === undefined ? 1 : hi, v));

  function pickWeights(pref) {
    if (isEmptyPreference(pref)) return POPULAR_FALLBACK_WEIGHTS;
    if (
      pref.calorie_limit !== null ||
      pref.min_protein !== null ||
      pref.dietary_tags.some((t) => NUTRITION_TAGS.includes(t))
    ) {
      return NUTRITION_FIRST_WEIGHTS;
    }
    return DEFAULT_WEIGHTS;
  }

  function weightedAverage(parts) {
    if (!parts.length) return NEUTRAL;
    const num = parts.reduce((s, p) => s + p[0] * p[1], 0);
    const den = parts.reduce((s, p) => s + p[1], 0);
    return clamp(num / den);
  }

  function spicyFit(level, tolerance) {
    if (tolerance <= 0) return level === 0 ? 1 : 0;
    if (level > tolerance) return 0.2;
    return 0.4 + 0.6 * (level / tolerance);
  }

  function scoreFlavor(dish, pref) {
    const parts = [];
    if (pref.liked_flavors.length) {
      const hit = pref.liked_flavors.filter((f) => dish.flavors.includes(f)).length;
      parts.push([hit / pref.liked_flavors.length, 2.0]);
    }
    if (pref.disliked_flavors.length) {
      const hit = pref.disliked_flavors.filter((f) => dish.flavors.includes(f)).length;
      parts.push([Math.max(0, 1 - hit * 0.7), 1.5]);
    }
    if (pref.spicy_tolerance !== null) {
      parts.push([spicyFit(dish.spicy_level, pref.spicy_tolerance), 1.0]);
    }
    return weightedAverage(parts);
  }

  function scoreCuisine(dish, pref) {
    if (!pref.liked_cuisines.length) return NEUTRAL;
    return pref.liked_cuisines.includes(dish.cuisine) ? 1.0 : 0.25;
  }

  function scorePrice(dish, pref) {
    const low = pref.budget_min;
    const high = pref.budget_max;
    if (high === null && low === null) return NEUTRAL;

    if (high !== null && low !== null) {
      if (low <= dish.price && dish.price <= high) return 1.0;
      const span = Math.max(high - low, 1.0);
      const gap = dish.price < low ? low - dish.price : dish.price - high;
      return clamp(1 - gap / span);
    }
    if (high !== null) {
      if (dish.price > high) return clamp(1 - (dish.price - high) / high) * 0.5;
      const ratio = dish.price / high;
      return ratio >= 0.5 ? 1.0 : 0.75 + 0.5 * ratio;
    }
    if (dish.price >= low) return 1.0;
    return low > 0 ? clamp(0.35 + 0.65 * (dish.price / low)) : 1.0;
  }

  function energyRatio(kcal, total) {
    return total > 0 ? kcal / total : 0;
  }

  function scoreNutrition(dish, pref) {
    const parts = [];
    const nut = dish.nutrition;
    const tags = pref.dietary_tags;

    if (pref.calorie_limit !== null && pref.calorie_limit > 0) {
      const ratio = nut.calories / pref.calorie_limit;
      const fit = ratio <= CALORIE_SWEET_SPOT
        ? 1.0
        : clamp(1 - (ratio - CALORIE_SWEET_SPOT) / CALORIE_DECAY_SPAN);
      parts.push([fit, 1.5]);
    }
    if (pref.min_protein !== null && pref.min_protein > 0) {
      parts.push([clamp(nut.protein / pref.min_protein), 1.5]);
    }
    if (tags.includes("高蛋白")) {
      const proteinRatio = nut.calories > 0 ? (nut.protein * 4) / nut.calories : 0;
      parts.push([clamp(proteinRatio / PROTEIN_RATIO_FULL), 1.5]);
    }
    if (tags.includes("低脂")) {
      parts.push([clamp(1 - energyRatio(nut.fat * 9, nut.calories) / FAT_RATIO_BAD), 1.0]);
    }
    if (tags.includes("低碳水") || tags.includes("低糖")) {
      parts.push([clamp(1 - energyRatio(nut.carbs * 4, nut.calories) / CARB_RATIO_BAD), 1.0]);
    }
    return weightedAverage(parts);
  }

  function scorePopularity(dish) {
    const shrunk =
      (dish.rating * dish.rating_count + RATING_PRIOR_SCORE * RATING_PRIOR_COUNT) /
      (dish.rating_count + RATING_PRIOR_COUNT);
    const ratingPart = clamp((shrunk - RATING_FLOOR) / (5.0 - RATING_FLOOR));
    const popularityPart = clamp(dish.popularity / POPULARITY_FULL);
    let base = 0.5 * ratingPart + 0.5 * popularityPart;
    if (dish.signature) base += 0.08;
    return clamp(base);
  }

  function scoreConvenience(dish, pref, crowdLevels) {
    const parts = [[clamp(1 - dish.wait_minutes / WAIT_TOLERABLE), 2.0]];
    if (crowdLevels) {
      const crowd = crowdLevels[dish.canteen];
      if (crowd !== undefined) parts.push([clamp(1 - crowd / CROWD_MAX), 1.0]);
    }
    if (pref.preferred_canteens.length) {
      parts.push([pref.preferred_canteens.includes(dish.canteen) ? 1.0 : 0.3, 1.0]);
    }
    if (pref.max_wait_minutes !== null && dish.wait_minutes > pref.max_wait_minutes) {
      parts.push([0.0, 1.5]);
    }
    return weightedAverage(parts);
  }

  // ---------- 规则版推荐理由（对应 scorer.build_reasons）----------

  /** 数字格式化，去掉多余的 .0，对应 Python 的 :g */
  const g = (n) => (Number.isInteger(n) ? String(n) : String(parseFloat(n.toFixed(2))));

  function nutritionReason(dish, pref) {
    const nut = dish.nutrition;
    const tags = pref.dietary_tags;
    if (tags.includes("高蛋白") || pref.min_protein !== null) {
      return `蛋白质 ${g(nut.protein)} 克，扛饿又顶练`;
    }
    if (tags.includes("低脂")) {
      return `脂肪只有 ${g(nut.fat)} 克，热量 ${g(nut.calories)} 千卡`;
    }
    if (tags.includes("低碳水") || tags.includes("低糖")) {
      return `碳水 ${g(nut.carbs)} 克，控糖友好`;
    }
    if (pref.calorie_limit !== null) {
      return `${g(nut.calories)} 千卡，离你 ${g(pref.calorie_limit)} 千卡上限还有余量`;
    }
    return `${g(nut.calories)} 千卡 / 蛋白质 ${g(nut.protein)} 克`;
  }

  function buildReasons(dish, pref, raw) {
    const reasons = [];

    if (raw.flavor >= HIGHLIGHT_RATIO) {
      const hit = pref.liked_flavors.filter((f) => dish.flavors.includes(f));
      if (hit.length) {
        reasons.push(`命中你想要的${hit.join("、")}口味`);
      } else if (pref.spicy_tolerance !== null && dish.spicy_level > 0) {
        reasons.push(`辣度 ${dish.spicy_level} 级，在你能接受的范围内`);
      }
    }
    if (raw.cuisine >= HIGHLIGHT_RATIO && pref.liked_cuisines.length) {
      reasons.push(`属于你偏好的${dish.cuisine}`);
    }
    if (raw.price >= HIGHLIGHT_RATIO) {
      if (pref.budget_max !== null) {
        reasons.push(`${g(dish.price)} 元，在 ${g(pref.budget_max)} 元预算内`);
      } else if (pref.budget_min !== null) {
        reasons.push(`${g(dish.price)} 元，够得上你想吃好点的预期`);
      }
    }
    if (raw.nutrition >= HIGHLIGHT_RATIO) {
      reasons.push(nutritionReason(dish, pref));
    }
    if (raw.popularity >= HIGHLIGHT_RATIO) {
      if (dish.signature) {
        reasons.push(`${dish.window}招牌菜，${dish.rating_count} 人评 ${g(dish.rating)} 分`);
      } else {
        reasons.push(`近七日卖出 ${dish.popularity} 份，${g(dish.rating)} 分口碑`);
      }
    }
    if (raw.convenience >= HIGHLIGHT_RATIO && dish.wait_minutes <= 5) {
      reasons.push(`预计只排 ${dish.wait_minutes} 分钟`);
    }

    return reasons.filter(Boolean).slice(0, MAX_REASONS);
  }

  /** 规则兜底推荐语（对应 comment_writer.fallback_comment）。 */
  function fallbackComment(rec) {
    if (rec.reasons.length) {
      return rec.reasons.slice(0, 2).join("，") + "。";
    }
    const d = rec.dish;
    return `${d.canteen}${d.window}，${g(d.price)} 元，${g(d.rating)} 分。`;
  }

  // ---------- 排序 ----------

  function scoreDish(dish, pref, weights, crowdLevels) {
    const raw = {
      flavor: scoreFlavor(dish, pref),
      cuisine: scoreCuisine(dish, pref),
      price: scorePrice(dish, pref),
      nutrition: scoreNutrition(dish, pref),
      popularity: scorePopularity(dish),
      convenience: scoreConvenience(dish, pref, crowdLevels),
    };

    const breakdown = {};
    let total = 0;
    for (const key of Object.keys(weights)) {
      const value = Math.round(raw[key] * weights[key] * 100) / 100;
      breakdown[key] = value;
      total += value;
    }

    const rec = {
      dish,
      score: Math.round(total * 100) / 100,
      breakdown,
      reasons: buildReasons(dish, pref, raw),
      comment: "",
    };
    rec.comment = fallbackComment(rec);
    return rec;
  }

  function rank(dishes, pref, crowdLevels, limit) {
    const weights = pickWeights(pref);
    const results = dishes.map((d) => scoreDish(d, pref, weights, crowdLevels));
    // 并列时按销量、评分、id 兜底，保证顺序可复现
    results.sort(
      (a, b) =>
        b.score - a.score ||
        b.dish.popularity - a.dish.popularity ||
        b.dish.rating - a.dish.rating ||
        (a.dish.id < b.dish.id ? -1 : a.dish.id > b.dish.id ? 1 : 0)
    );
    return limit ? results.slice(0, limit) : results;
  }

  function rankDiverse(dishes, pref, crowdLevels, limit, maxPerWindow) {
    limit = limit || 5;
    maxPerWindow = maxPerWindow || 2;
    const ranked = rank(dishes, pref, crowdLevels);

    const picked = [];
    const deferred = [];
    const seen = {};

    for (const rec of ranked) {
      const key = rec.dish.canteen + "|" + rec.dish.window;
      if ((seen[key] || 0) >= maxPerWindow) {
        deferred.push(rec);
        continue;
      }
      seen[key] = (seen[key] || 0) + 1;
      picked.push(rec);
      if (picked.length >= limit) return picked;
    }

    return picked.concat(deferred.slice(0, Math.max(0, limit - picked.length)));
  }

  // ---------- 编排（对应 orchestrator.py）----------

  const MEAL_PLAN_CATEGORIES = ["主食", "荤菜", "素菜"];
  const EMPTY_MESSAGE =
    "按你的条件没找到能吃的菜。过敏原和饮食限制是硬性要求不会放宽，" +
    "可以试着放宽预算或换个食堂看看。";

  /** 合并表单偏好到解析结果。表单是用户明确勾的，优先级高于文本解析。 */
  function mergePreference(base, override) {
    if (!override) return base;
    const merged = Object.assign({}, base);
    for (const key of Object.keys(override)) {
      const value = override[key];
      if (value === null || value === undefined) continue;
      if (Array.isArray(value)) {
        if (!value.length) continue;
        // 数组做并集，保留文本里解析出的条件
        const existing = Array.isArray(merged[key]) ? merged[key] : [];
        merged[key] = Array.from(new Set(existing.concat(value)));
      } else {
        merged[key] = value;
      }
    }
    // 合并后可能出现同一口味既喜欢又讨厌，仍以喜欢为准
    if (merged.liked_flavors && merged.disliked_flavors) {
      merged.disliked_flavors = merged.disliked_flavors.filter(
        (f) => !merged.liked_flavors.includes(f)
      );
    }
    return merged;
  }

  function buildMealPlan(candidates, pref, crowdLevels) {
    const byCategory = {};
    for (const dish of candidates) {
      (byCategory[dish.category] = byCategory[dish.category] || []).push(dish);
    }

    const items = [];
    for (const category of MEAL_PLAN_CATEGORIES) {
      const dishes = byCategory[category];
      if (!dishes || !dishes.length) continue;
      const best = rank(dishes, pref, crowdLevels, 1);
      if (best.length) items.push(best[0]);
    }
    if (items.length < 2) return null;

    const totalPrice = items.reduce((s, i) => s + i.dish.price, 0);
    const totalCalories = items.reduce((s, i) => s + i.dish.nutrition.calories, 0);
    return {
      items,
      total_price: Math.round(totalPrice * 100) / 100,
      total_calories: Math.round(totalCalories * 10) / 10,
      summary: `${items.length} 道菜共 ${g(totalPrice)} 元、${g(totalCalories)} 千卡，主食配菜都齐了。`,
    };
  }

  /**
   * 演示版推荐，返回结构与后端 RecommendResponse 完全一致，
   * 这样 app.js 的渲染代码不用区分数据来源。
   */
  function recommend(options) {
    const opts = options || {};
    const text = (opts.text || "").trim();
    const limit = opts.limit || 5;
    const dishes = opts.dishes || global.DEMO_DISHES || [];
    const canteens = opts.canteens || global.DEMO_CANTEENS || [];

    const crowdLevels = {};
    canteens.forEach((c) => (crowdLevels[c.name] = c.crowd_level));

    let pref = parseByKeywords(text);
    pref = mergePreference(pref, opts.preference);
    pref.raw_text = text;

    const notes = [];
    // 演示引擎里没有大模型，等价于后端降级路径，所以恒为 true
    if (text) notes.push("演示模式：未接入千帆，已按关键词理解你的需求");

    const found = findCandidates(dishes, pref, 5);
    notes.push(...found.notes);

    if (!found.candidates.length) {
      return {
        recommendations: [],
        meal_plan: null,
        parsed_preference: compactPreference(pref),
        total_candidates: 0,
        fallback_used: true,
        message: EMPTY_MESSAGE,
      };
    }

    const picked = rankDiverse(found.candidates, pref, crowdLevels, limit, 2);

    return {
      recommendations: picked,
      meal_plan: opts.with_meal_plan
        ? buildMealPlan(found.candidates, pref, crowdLevels)
        : null,
      parsed_preference: compactPreference(pref),
      total_candidates: found.candidates.length,
      fallback_used: true,
      message: notes.join("；"),
    };
  }

  /** 去掉空值，对应后端 model_dump(exclude_defaults=True) 的效果。 */
  function compactPreference(pref) {
    const out = {};
    for (const key of Object.keys(pref)) {
      const value = pref[key];
      if (value === null || value === undefined || value === "") continue;
      if (Array.isArray(value) && !value.length) continue;
      if (key === "raw_text") continue;
      out[key] = value;
    }
    return out;
  }

  // ---------- 菜品查询（对应 routes.list_dishes）----------

  function listDishes(query) {
    const q = query || {};
    const all = q.dishes || global.DEMO_DISHES || [];
    let dishes = q.include_unavailable ? all.slice() : all.filter((d) => d.available);

    if (q.keyword) {
      const kw = q.keyword.trim();
      if (kw) {
        dishes = dishes.filter(
          (d) =>
            d.name.includes(kw) ||
            (d.description || "").includes(kw) ||
            d.ingredients.some((i) => i.includes(kw))
        );
      }
    }
    if (q.canteen) dishes = dishes.filter((d) => d.canteen === q.canteen);
    if (q.category) dishes = dishes.filter((d) => d.category === q.category);
    if (q.cuisine) dishes = dishes.filter((d) => d.cuisine === q.cuisine);
    if (q.meal_period) dishes = dishes.filter((d) => d.meal_periods.includes(q.meal_period));

    return { dishes, total: dishes.length };
  }

  global.DemoEngine = {
    recommend,
    listDishes,
    parseByKeywords,
    findCandidates,
    rank,
    rankDiverse,
    pickWeights,
    buildReasons,
    fallbackComment,
    isEmptyPreference,
    WEIGHTS: {
      default: DEFAULT_WEIGHTS,
      nutritionFirst: NUTRITION_FIRST_WEIGHTS,
      popularFallback: POPULAR_FALLBACK_WEIGHTS,
    },
  };
})(typeof window !== "undefined" ? window : globalThis);
