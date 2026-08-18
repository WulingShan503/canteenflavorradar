/* 把后端示例数据打包成前端可直接 <script> 引入的 JS。
 *
 * 为什么不让页面直接 fetch JSON：用 file:// 打开时浏览器会把本地文件当跨域拒掉，
 * 而这个页面的一个目标就是「双击就能看」。内联成 JS 变量最省事。
 *
 * 用法：node scripts/build-demo-data.js
 * 改了 backend/app/data/*.json 之后记得跑一遍。
 */

const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const DATA_DIR = path.join(ROOT, "backend", "app", "data");
const OUT_FILE = path.join(ROOT, "frontend", "js", "demo-data.js");

function read(name) {
  const file = path.join(DATA_DIR, name);
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

const canteens = read("canteens.json");
const dishes = read("dishes.json");

// 挂到 globalThis 而不是用 const：顶层 const 不会成为 window 的属性，
// demo-engine.js 里的 global.DEMO_DISHES 就取不到了。
const header = `/* 演示用数据快照，由 backend/app/data/*.json 生成，不要手改。
 * 重新生成：node scripts/build-demo-data.js
 * 内联成 JS 是为了让页面用 file:// 直接打开也能跑（fetch 本地 JSON 会被 CORS 挡住）。
 */
(function (global) {
  "use strict";
`;

const footer = `})(typeof window !== "undefined" ? window : globalThis);\n`;

const body =
  header +
  "  global.DEMO_CANTEENS = " +
  JSON.stringify(canteens, null, 2) +
  ";\n\n  global.DEMO_DISHES = " +
  JSON.stringify(dishes, null, 2) +
  ";\n" +
  footer;

fs.mkdirSync(path.dirname(OUT_FILE), { recursive: true });
fs.writeFileSync(OUT_FILE, body, "utf8");

console.log(
  `已生成 ${path.relative(ROOT, OUT_FILE)}：${dishes.length} 道菜、${canteens.length} 个食堂`
);
