/* 数据来源适配层。
 *
 * 后端起着就用真实接口，起不来就退到内置的演示引擎。
 * 这么做是为了让页面在两种场景下都能看：
 *   1. 开发/评审时跑着 uvicorn，走真实 Agent（有千帆密钥时还有大模型推荐语）；
 *   2. 直接双击 index.html，没有任何服务，也能看到完整的过滤打分效果。
 *
 * 上层 app.js 拿到的响应结构在两种模式下完全一致，不用写分支。
 */

(function (global) {
  "use strict";

  // file:// 打开时没有同源后端可言，直接进演示模式，省掉一次注定失败的请求
  const IS_FILE = global.location && global.location.protocol === "file:";

  // 同源部署时留空即可；前后端分离时改成后端地址，如 http://127.0.0.1:8000
  const API_BASE = "";

  const PROBE_TIMEOUT = 1500; // 探活别让用户干等

  const state = {
    mode: IS_FILE ? "demo" : "unknown", // 'live' | 'demo' | 'unknown'
    health: null,
  };

  function url(path) {
    return API_BASE + path;
  }

  async function fetchWithTimeout(path, options, timeout) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout || 8000);
    try {
      return await fetch(url(path), Object.assign({ signal: controller.signal }, options));
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * 探测后端是否可用。只在启动时调一次，结果缓存在 state 里。
   * 探不到不是错误，是正常的演示场景。
   */
  async function probe() {
    if (IS_FILE) {
      state.mode = "demo";
      return state;
    }
    try {
      const resp = await fetchWithTimeout("/api/health", {}, PROBE_TIMEOUT);
      if (!resp.ok) throw new Error("health " + resp.status);
      state.health = await resp.json();
      state.mode = "live";
    } catch (err) {
      state.mode = "demo";
      state.health = null;
    }
    return state;
  }

  /** 统一的错误信息提取，后端 422/500 都返回 {detail, code}。 */
  async function readError(resp) {
    try {
      const body = await resp.json();
      return body.detail || body.message || `请求失败（HTTP ${resp.status}）`;
    } catch (err) {
      return `请求失败（HTTP ${resp.status}）`;
    }
  }

  async function recommend(payload) {
    if (state.mode === "demo") {
      return global.DemoEngine.recommend(payload);
    }
    const resp = await fetchWithTimeout("/api/recommend", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      // 422 是用户输入问题，要原样告诉他；其他错误退到演示引擎，
      // 总比让页面白屏好——反正规则层的结果是一样的。
      if (resp.status === 422) throw new Error(await readError(resp));
      state.mode = "demo";
      return global.DemoEngine.recommend(payload);
    }
    return resp.json();
  }

  async function listDishes(query) {
    if (state.mode === "demo") {
      return global.DemoEngine.listDishes(query || {});
    }
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query || {})) {
      if (value === null || value === undefined || value === "" || value === false) continue;
      params.set(key, String(value));
    }
    const suffix = params.toString() ? "?" + params.toString() : "";
    const resp = await fetchWithTimeout("/api/dishes" + suffix);
    if (!resp.ok) {
      state.mode = "demo";
      return global.DemoEngine.listDishes(query || {});
    }
    return resp.json();
  }

  async function listCanteens() {
    if (state.mode === "demo") {
      const canteens = global.DEMO_CANTEENS || [];
      return { canteens, total: canteens.length };
    }
    const resp = await fetchWithTimeout("/api/canteens");
    if (!resp.ok) {
      const canteens = global.DEMO_CANTEENS || [];
      return { canteens, total: canteens.length };
    }
    return resp.json();
  }

  global.Api = {
    probe,
    recommend,
    listDishes,
    listCanteens,
    get mode() {
      return state.mode;
    },
    get health() {
      return state.health;
    },
  };
})(typeof window !== "undefined" ? window : globalThis);
