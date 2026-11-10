// EvilTown 前端渲染层：REST 拉静态地图，WebSocket 推帧叠加动态元素。
'use strict';

const $ = (id) => document.getElementById(id);
// 全局语言（web/i18n.js）：T() 取字典文案，L(中文, English) 按当前语言二选一。
const T = (key, vars) => window.i18n.t(key, vars);
const L = (zh, en) => window.i18n.L(zh, en);
const canvas = $('map');
const ctx = canvas.getContext('2d');
const tooltip = $('tooltip');

let MAP = null;         // /api/map 响应
let STATE = null;       // 最近一帧
let staticLayer = null; // 离屏静态图层
let WS = null;
// 是否在居民点内显示姓名（顶栏「名字」勾选框控制，默认开）。
// 关掉后回落为原来的「高光小圆点」，便于极密集时看整体人流。
let SHOW_NAMES = true;

// —— 地图显示缩放（相对画布真实像素的倍数）——
// 画布真实尺寸 = world(240×160) × tile_px(24) = 5760×3840，远大于舞台可视区，
// 所以**打开时先缩到「整图适应窗口」**，再由用户按需放大。
// 底层 ZOOM 是相对画布真实像素的倍率；**顶栏显示的百分比一律相对「整图俯瞰」**：
// 100% = 整张地图刚好装进舞台，>100% = 放大，<100% = 缩小留白。
let ZOOM = 1;
let ZOOM_FIT = 0.3;          // 100% 基准（fitScale() 按实际舞台尺寸覆盖，勿写死）
const ZOOM_MIN_MULT = 0.4;   // 最小 = 整图俯瞰的 40%
const ZOOM_MAX_MULT = 6;     // 最大 = 整图俯瞰的 600%（再高会糊，见 syncZoomRange）
// 绝对上下限：由 syncZoomRange() 依 ZOOM_FIT 重算，这里只是初始化兜底
let ZOOM_MIN = 0.05;
let ZOOM_MAX = 3;
// 用户是否手动缩放过：没手动过 → 窗口 resize 时自动重新适配全貌
let ZOOM_MANUAL = false;

// 居民点绘制半径（画布像素）。与 residentAt() 的点击容差共用，避免两处数值漂移。
// 用户反馈「人太小、点不中、看不清名字」：11 → 14 → 21（tile_px 16→24 后按 1.5 倍同步）。
const R_RES = 21;
// 家具点位配色：与点位编辑器（web/furniture-editor.html 的 PTYPES）保持一致。
// 以前地图上除床/马桶外一律画成同一种黄，编辑器里分得清、游戏里分不清（2026-11-09 修）。
const FURN_COLORS = {
  toilet: '#ffffff',   // 马桶：白点（沿用原画法）
  bed: '#7fd1ff',      // 床：浅蓝方块（沿用原画法）
  table: '#ff9f5a',    // 桌 / 吃饭
  desk: '#9d7bff',     // 工位 / 办公
  seat: '#66e0a3',     // 座位 / 休息
  counter: '#ff6b8a',  // 柜台 / 收银
  door: '#e8e8e8',     // 入口 / 门
  idle: '#7a8c99',     // 闲逛热点
};
// 点位类型的中英名：与 web/furniture-editor.html 的 PTYPES 同一套 id 与颜色。
// 两边色表必须一致（改这里要同步改编辑器），tools/_apply_and_check_points.py 会逐项比对。
const POINT_CN = { toilet: '马桶', bed: '床', table: '桌', desk: '工位',
  seat: '座位', counter: '柜台', door: '门口', idle: '闲逛热点' };
const POINT_EN = { toilet: 'Toilet', bed: 'Bed', table: 'Table', desk: 'Desk',
  seat: 'Seat', counter: 'Counter', door: 'Door', idle: 'Idle spot' };

const RING = Math.max(2, R_RES * 0.2);   // 深色外圈随半径等比放大

// 每栋楼的贴图放大倍数（drawStatic 里算好后存这儿）。建筑内的居民、马桶白点、
// 家具点位必须走同一个映射 screenOfTile()，否则会与放大的贴图图案错位。
let S_OF = [];

// 把画布显示尺寸设为 zoom 倍（不改 canvas.width/height 真实像素，只改 CSS 显示尺寸）。
// 点击/悬停的坐标换算走 tileFromEvent() 的 getBoundingClientRect()，会自动适配任何缩放比。
function applyZoom() {
  if (!canvas.width) return;
  canvas.style.width = Math.round(canvas.width * ZOOM) + 'px';
  canvas.style.height = Math.round(canvas.height * ZOOM) + 'px';
  const lab = $('zoom-val');
  // 百分比相对「整图俯瞰」：100% = 一屏装下整张地图，放大才 >100%
  if (lab) lab.textContent = Math.round((ZOOM / (ZOOM_FIT || 1)) * 100) + '%';
}

function setZoom(z) {
  ZOOM = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z));
  applyZoom();
}

// 算「整图刚好装进舞台」的倍率：宽、高都要满足，且只缩不放（上限 1）。
function fitScale() {
  const stage = document.querySelector('.stage');
  if (!stage || !canvas.width || !canvas.height) return 0;
  const legend = stage.querySelector('.legend');
  const legendH = legend ? legend.offsetHeight + 8 : 0;  // 图例占高 + .stage 的 gap:8px
  const availW = Math.max(80, stage.clientWidth - 24);   // 减去 .stage 左右 padding(12×2)
  const availH = Math.max(80, stage.clientHeight - 24 - legendH);
  return Math.min(availW / canvas.width, availH / canvas.height, 1);
}

// 依当前舞台尺寸刷新「100% 基准」与缩放上下限，但**不改当前视图**。
// 100% = 整图俯瞰；下限 40%（四周留白）；上限 600% 且不超过原始像素 3 倍。
function syncZoomRange() {
  const s = fitScale();
  if (!s) return 0;                       // 舞台还没布局好：保持旧基准
  ZOOM_FIT = s;
  ZOOM_MIN = s * ZOOM_MIN_MULT;
  // 上限锁在「画布像素 1:1」：再往上就是拉伸位图，只会糊（用户反馈「放大也模糊」）。
  // 之所以还乘 ZOOM_MAX_MULT：屏幕很大时 fit 本身已接近 1，1:1 即视野上限。
  ZOOM_MAX = Math.max(s, Math.min(1, s * ZOOM_MAX_MULT));
  return s;
}

// 缩放到「整图俯瞰」= 100%：整张地图（宽和高）刚好放进舞台，从左上对齐。
function fitZoom() {
  const s = syncZoomRange();
  if (!s) return;
  ZOOM_MANUAL = false;
  setZoom(s);
  const stage = document.querySelector('.stage');
  if (stage) { stage.scrollLeft = 0; stage.scrollTop = 0; }
}

// 以鼠标位置为锚点缩放：缩放后鼠标下面那个点仍留在屏幕原处（地图类应用惯例）。
// 省略 cx/cy 时以舞台中心为锚点（供 －/＋ 按钮使用）。
function zoomAt(factor, cx, cy) {
  const stage = document.querySelector('.stage');
  if (!stage || !canvas.width) return;
  if (cx == null) {
    const sr = stage.getBoundingClientRect();
    cx = sr.left + sr.width / 2;
    cy = sr.top + sr.height / 2;
  }
  const rect = canvas.getBoundingClientRect();
  const mx = cx - rect.left;            // 锚点相对画布左上角（显示像素）
  const my = cy - rect.top;
  const old = ZOOM;
  const next = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, old * factor));
  if (Math.abs(next - old) < 1e-6) return;   // 已到上下限：不做无意义的滚动补偿
  ZOOM_MANUAL = true;
  setZoom(next);
  // 内容坐标 = scroll + 视口内偏移；按比例 k 反推新的 scroll，锚点就不漂移
  const k = next / old;
  stage.scrollLeft = (stage.scrollLeft + mx) * k - mx;
  stage.scrollTop = (stage.scrollTop + my) * k - my;
}

// —— 按住鼠标拖动平移地图 ——
// 放大后画布（5760×3840）远大于舞台，只能拉滚动条太别扭（触摸板尤其难用）。
// 与「点建筑 / 选居民」共存：位移 < PAN_START_PX 仍算点击，超过才算拖拽；
// 拖拽结束 300ms 内的那次 click 会被吞掉（见 boot 里的 canvas.onclick）。
const PAN_START_PX = 4;
let PAN = null;          // { sx, sy, sl, st, moved }
let _panEndAt = -1e9;    // 上次拖拽结束时刻（performance.now）

function panMoved(e) {
  return !!PAN && Math.hypot(e.clientX - PAN.sx, e.clientY - PAN.sy) >= PAN_START_PX;
}

function panStart(e) {
  if (!canvas.width) return;
  if (e.button !== 0 && e.button !== 1) return;   // 左键 / 中键；右键留给浏览器菜单
  const stage = document.querySelector('.stage');
  if (!stage) return;
  if (e.button === 1) e.preventDefault();         // 掐掉中键的「自动滚动」模式
  PAN = { stage: stage, sx: e.clientX, sy: e.clientY, sl: stage.scrollLeft, st: stage.scrollTop, moved: false };
}

function panMove(e) {
  if (!PAN) return;
  if (!PAN.moved) {
    if (!panMoved(e)) return;
    PAN.moved = true;
    canvas.style.cursor = 'grabbing';
    hideTooltip();
  }
  const stage = PAN.stage;
  if (!stage) return;
  stage.scrollLeft = PAN.sl - (e.clientX - PAN.sx);
  stage.scrollTop = PAN.st - (e.clientY - PAN.sy);
  e.preventDefault();
}

function panEnd() {
  if (!PAN) return;
  if (PAN.moved) { _panEndAt = performance.now(); canvas.style.cursor = ''; }
  PAN = null;
}

// 地形配色（按 legend 名称取，避免硬编码枚举值）
const TERRAIN_COLORS = {
  GRASS: '#f4e8cd', ROAD: '#f4e8cd', WATER: '#2f5d8a', TREE: '#254a24',   // 地面＝沙子奶黄白；道路并入地面、不单独上色
  SAND: '#c2b280', FLOOR: '#8a7a63', WALL: '#3a3a3a',
};
// 地形中文名（图例「中文在前、英文在后」用），后端枚举值本身是英文名
const TERRAIN_CN = {
  GRASS: '草地', ROAD: '道路', WATER: '水域', TREE: '树木',
  SAND: '沙地', FLOOR: '地板', WALL: '墙壁',
};
const TERRAIN_EN = {
  GRASS: 'Grass', ROAD: 'Road', WATER: 'Water', TREE: 'Tree',
  SAND: 'Sand', FLOOR: 'Floor', WALL: 'Wall',
};

// 装饰性地形：仅作背景点缀，逐格渲染会变成扎眼的「点点」。
// 渲染时并入草地底色（视觉隐藏），但仿真逻辑不变——沙地仍可走、树木仍阻挡。
const TERRAIN_DECOR = new Set(['TREE', 'SAND', 'ROAD']);   // ROAD：灰路像「棒棒糖棍」，并入地面且图例中不列出

// 建筑配色：按 building.kind（见 eviltown/world/buildings.py::BuildingKind）上色。
// 数据在、肉眼分不出 = B3 根因；此处给每类业态一个高区分度的填充色。
const BUILDING_COLORS = {
  home: '#d98b6a',        // 住宅：暖砖红
  office: '#4f7fd6',      // 办公楼：蓝
  school: '#e0b23c',      // 学校：金黄
  restaurant: '#d9534f',  // 餐馆：红
  canteen: '#c96a2b',     // 食堂：橙
  shop: '#8f6fd6',        // 商店：紫
  park: '#4fae63',        // 公园：绿
  toilet: '#8fa6b8',      // 公共厕所：灰蓝
  hospital: '#e8e8e8',    // 诊所：白
  bar: '#b0703c',         // 酒吧：琥珀棕
  fishing_pond: '#3f9fd6',// 钓鱼池塘：水蓝
};
const BUILDING_KIND_CN = {
  home: '住宅', office: '办公楼', school: '学校', restaurant: '餐馆',
  canteen: '食堂', shop: '商店', park: '公园', toilet: '公共厕所', hospital: '诊所',
  bar: '酒吧', fishing_pond: '钓鱼池塘',
};
// 颜色中文名：图例里以文字形式标出「建筑 → 颜色」，即使色块不渲染也能读懂
const BUILDING_COLOR_CN = {
  home: '砖红', office: '蓝', school: '金黄', restaurant: '红',
  canteen: '橙', shop: '紫', park: '绿', toilet: '灰蓝', hospital: '白',
  bar: '琥珀', fishing_pond: '水蓝',
};
// 英文对照（图例改为「中文 · 颜色 / English · color」两行）
const BUILDING_KIND_EN = {
  home: 'Home', office: 'Office', school: 'School', restaurant: 'Restaurant',
  canteen: 'Canteen', shop: 'Shop', park: 'Park', toilet: 'Public toilet', hospital: 'Clinic',
  bar: 'Bar', fishing_pond: 'Fishing pond',
};
const BUILDING_COLOR_EN = {
  home: 'Brick red', office: 'Blue', school: 'Gold', restaurant: 'Red',
  canteen: 'Orange', shop: 'Purple', park: 'Green', toilet: 'Slate', hospital: 'White',
  bar: 'Amber', fishing_pond: 'Water blue',
};

function b64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

const NAME_BY_VALUE = {}; // Terrain 值 -> 名称

// —— 建筑贴图与家具点位 ——
// 贴图清单：/static/assets/buildings/manifest.json 的 kinds[业态] = [相对路径, ...]
// 点位：/api/furniture-points 返回的 points.json
//       v2 结构 kinds[业态].images[贴图相对路径] = { grid, points: [{type, fx, fy}] }
// 建筑按「同业态出现顺序」轮流取图；图没加载完时先显示色块，加载完自动重绘。
let BUILDING_TEX = []; // 与 MAP.buildings 同序：{ img, rel } 或 null
let MAP_POINTS = null; // points.json 的 kinds 段

// 按「业态 + 贴图」取点位；取不到返回空数组（该业态还没摆点，或这张图没点）
function pointsOf(kind, rel) {
  const kindBox = MAP_POINTS && MAP_POINTS[kind];
  const imgs = kindBox && kindBox.images;
  if (!imgs || !rel) return [];
  const hit = imgs[rel] || imgs[rel.replace(/^.*\//, '')] || null;
  return (hit && hit.points) || [];
}

function loadMapDecor() {
  const pTex = fetch('/static/assets/buildings/manifest.json')
    .then((r) => (r.ok ? r.json() : null))
    .catch(() => null);
  const pPts = fetch('/api/furniture-points')
    .then((r) => (r.ok ? r.json() : null))
    .catch(() => null);

  Promise.all([pTex, pPts]).then(([man, pts]) => {
    MAP_POINTS = (pts && pts.kinds) || null;
    const kinds = (man && man.kinds) || {};
    const used = {};
    const out = [];
    let pending = 0;
    const one = () => {
      if (--pending > 0) return;
      drawStatic(); // 重建静态层：贴图与点位覆盖到色块上
      drawFrame();
    };
    for (const b of MAP.buildings || []) {
      const arr = kinds[b.kind] || [];
      if (!arr.length) { out.push(null); continue; } // 这个业态没有贴图，保持色块
      const n = used[b.kind] || 0;
      used[b.kind] = n + 1;
      // 以后端为准：b.tex = 这栋楼实际绑定家具用的那张图
      const rel = b.tex || arr[n % arr.length];
      const img = new Image();
      img.onload = one;
      img.onerror = one; // 单张挂了就当没有，不阻塞其它建筑
      pending += 1;
      img.src = '/static/assets/buildings/' + rel;
      out.push({ img, rel });
    }
    BUILDING_TEX = out;
    if (pending === 0) { drawStatic(); drawFrame(); } // 一张图都没有
  });
}

// —— 墙格消隐（2026-11-09 用户指定：用「贴图自己的边缘线」做建筑轮廓）——
// 建筑外圈那一层 WALL 格原本画成深灰 #3a3a3a，贴图边缘透明处会透出来，
// 视觉上就是房子背后「横竖的灰色杠杠」。现在改为按附近地面色填充，
// 房子的边界完全交给贴图自身（配合下面 TEX_SHADOW 的淡影）。
// 想恢复旧的灰墙观感：WALL_BLEND = false。
const WALL_BLEND = false;  // 实测：用户看到的灰条是 ROAD（后端铺路），墙格不处理。保留开关备查
const TEX_SHADOW = false;  // 关：shadowBlur 会在贴图不透明边缘糊出一圈深色，看着像黑框

// 取 (x,y) 附近最近的非 WALL 地形名：由近到远扫 3 圈 8 邻域。
// 全被 WALL 包住时回落 FLOOR（室内地面），避免房子内部冒出绿斑。
function groundNear(bytes, w, h, x, y) {
  for (let r = 1; r <= 3; r++) {
    for (let dy = -r; dy <= r; dy++) {
      for (let dx = -r; dx <= r; dx++) {
        if (Math.max(Math.abs(dx), Math.abs(dy)) !== r) continue;
        const nx = x + dx, ny = y + dy;
        if (nx < 0 || ny < 0 || nx >= w || ny >= h) continue;
        const n = NAME_BY_VALUE[bytes[ny * w + nx]] || '';
        if (n && n !== 'WALL') return TERRAIN_DECOR.has(n) ? 'GRASS' : n;
      }
    }
  }
  return 'FLOOR';
}

// 贴图放大上限（逐栋算）：放大后不与任何邻居/画布边相交。
// 贴图以建筑中心等比放大 S 倍 ⇒ 每边外扩 尺寸·(S−1)/2。
// 两家之间的间隙必须**平分**（各吃一半），所以 S ≤ 1 + gap/尺寸；
// 分离方向取「间隙更大的那一维」，双方对同一对数算出同一结论，不会各让一边还撞上。
// 旧实现是全局固定 1.4 倍，两栋只隔 2 格（48px）的房子会互相压盖 —— 这就是「遮挡」的来源。
function gapScale(b, list, tp, cw, ch) {
  const pw = b.w * tp, ph = b.h * tp;
  const cx = (b.x + b.w / 2) * tp, cy = (b.y + b.h / 2) * tp;
  // 画布边界：放大后不能出图
  let cap = Math.min(2 * cx / pw, 2 * (cw - cx) / pw,
                     2 * cy / ph, 2 * (ch - cy) / ph);
  for (const o of list) {
    if (o === b) continue;
    // 水平 / 垂直间隙（负数 = 本来投影就重叠）
    const gx = Math.max(o.x - (b.x + b.w), b.x - (o.x + o.w)) * tp;
    const gy = Math.max(o.y - (b.y + b.h), b.y - (o.y + o.h)) * tp;
    const s = gx >= gy ? 1 + gx / pw : 1 + gy / ph;
    cap = Math.min(cap, s);
  }
  return Math.max(1, cap);   // 下限 1：至少铺满自己的占地，不会缩得更小
}

function drawStatic() {
  const w = MAP.width, h = MAP.height, tp = MAP.tile_px;
  canvas.width = w * tp;
  canvas.height = h * tp;

  const off = document.createElement('canvas');
  off.width = canvas.width;
  off.height = canvas.height;
  const octx = off.getContext('2d');
  const bytes = b64ToBytes(MAP.tiles);

  // 建筑占地格的地形（FLOOR 棕褐 / WALL 深灰 #3a3a3a）一律按草地画。
  // 贴图四边 100% 透明、又正好铺满建筑矩形，四周必然露出底下的地形色 ——
  // 那一圈深灰 #3a3a3a 就是看到的「黑框」，本质是建筑的占地边界。
  const IN_BUILDING = new Set();
  for (const b of MAP.buildings || []) {
    for (let by = b.y; by < b.y + b.h; by++) {
      for (let bx = b.x; bx < b.x + b.w; bx++) IN_BUILDING.add(by * w + bx);
    }
  }

  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      let name = NAME_BY_VALUE[bytes[y * w + x]] || '';
      if (TERRAIN_DECOR.has(name)) name = 'GRASS'; // 树/沙并入草地，去掉「点点」
      if (WALL_BLEND && name === 'WALL') name = groundNear(bytes, w, h, x, y); // 墙格消隐
      if (IN_BUILDING.has(y * w + x)) name = 'GRASS'; // 占地内别让深灰墙格透出成黑框
      octx.fillStyle = TERRAIN_COLORS[name] || '#000';
      octx.fillRect(x * tp, y * tp, tp, tp);
    }
  }

  // 建筑贴图：只保留贴图，不再铺色块底。放大倍数**逐栋自适应**（见 gapScale）：
  // 空旷处最多 TEX_SCALE_MAX 倍，挤在一起的自然收敛，保证谁也不压到邻居。
  const TEX_SCALE_MAX = 2.9;   // 用户反馈「房子太小」：2.2 → 2.9（仍受邻居间隙约束）
  S_OF = (MAP.buildings || []).map((b, i) => {
    const tex = BUILDING_TEX[i];
    const nw = (tex && tex.img && tex.img.naturalWidth) || 1024;
    const nh = (tex && tex.img && tex.img.naturalHeight) || 1024;
    return Math.max(1, Math.min(
      TEX_SCALE_MAX,
      gapScale(b, MAP.buildings, tp, canvas.width, canvas.height),
      nw / Math.max(1, b.w * tp), nh / Math.max(1, b.h * tp),   // 不超过贴图 1:1，免拉糊
    ));
  });
  (MAP.buildings || []).forEach((b, i) => {
    const tex = BUILDING_TEX[i];
    if (!tex || !tex.img || !tex.img.complete || !tex.img.naturalWidth) return;
    const cx = (b.x + b.w / 2) * tp, cy = (b.y + b.h / 2) * tp;
    const s = S_OF[i] || 1;
    const dw = b.w * tp * s, dh = b.h * tp * s;
    // 淡影沿贴图自身的不透明轮廓走，房子像立在地面上；不再靠灰墙条勾边。
    if (TEX_SHADOW) {
      octx.save();
      octx.shadowColor = 'rgba(0,0,0,.45)';
      octx.shadowBlur = Math.max(6, tp * 0.7);
      octx.shadowOffsetY = Math.max(2, tp * 0.3);
      octx.drawImage(tex.img, cx - dw / 2, cy - dh / 2, dw, dh);
      octx.restore();
    } else {
      octx.drawImage(tex.img, cx - dw / 2, cy - dh / 2, dw, dh);
    }
  });

  // 马桶白点（用户规格）：建筑内每个马桶格画一个白色小圆点。
  // 家 / 各用人单位各 2 个，公共厕所 5~10 个，由服务端 buildings 数据给出。
  const R_TOILET = Math.max(1.2, tp * 0.28);
  (MAP.buildings || []).forEach((b, i) => {
    const s = S_OF[i] || 1;
    for (const t of b.toilets || []) {
      const bcx = (b.x + b.w / 2) * tp, bcy = (b.y + b.h / 2) * tp;
      const px = bcx + ((t[0] + 0.5) * tp - bcx) * s;
      const py = bcy + ((t[1] + 0.5) * tp - bcy) * s;
      octx.beginPath();
      octx.arc(px, py, R_TOILET, 0, Math.PI * 2);
      octx.fillStyle = '#ffffff';
      octx.fill();
      octx.lineWidth = 1;
      octx.strokeStyle = 'rgba(20,24,32,.75)';
      octx.stroke();
    }
  });
  // 家具点位：坐标是相对贴图的归一化 fx/fy，换算到建筑矩形内的像素位置。
  // 马桶白点已由上面 b.toilets 单独画过，这里跳过 toilet，只画床 / 桌 / 椅等。
  const PR = Math.max(1, tp * 0.24);
  (MAP.buildings || []).forEach((b, i) => {
    const tex = BUILDING_TEX[i];
    if (!tex) return;
    const s2 = S_OF[i] || 1;
    for (const p of pointsOf(b.kind, tex.rel)) {
      if (!p || p.type === 'toilet') continue;
      const bcx = (b.x + b.w / 2) * tp, bcy = (b.y + b.h / 2) * tp;
      const dw2 = b.w * tp * s2, dh2 = b.h * tp * s2;
      const px = bcx - dw2 / 2 + (p.fx || 0) * dw2;
      const py = bcy - dh2 / 2 + (p.fy || 0) * dh2;
      if (p.type === 'bed') {
        octx.fillStyle = '#7fd1ff';
        octx.fillRect(px - PR, py - PR, PR * 2, PR * 2);
      } else {
        octx.beginPath();
        octx.arc(px, py, PR, 0, Math.PI * 2);
        octx.fillStyle = FURN_COLORS[p.type] || FURN_COLORS.idle;
        octx.fill();
      }
    }
  });

  staticLayer = off;
}

// 建筑图例：固定挂在右侧信息面板（#building-legend）上，只列实际出现的业态
function renderBuildingLegend() {
  const box = $('building-legend');
  if (!box) return;
  box.innerHTML = '';
  const present = [];
  for (const b of MAP.buildings || []) {
    if (b.kind && present.indexOf(b.kind) < 0) present.push(b.kind);
  }
  for (const kind of present) {
    const item = document.createElement('div');
    item.className = 'item';
    const sw = document.createElement('span');
    sw.className = 'sw';
    sw.style.background = BUILDING_COLORS[kind] || '#9aa2b1';
    item.appendChild(sw);
    const cn = BUILDING_KIND_CN[kind] || kind;
    const colorCn = BUILDING_COLOR_CN[kind] || '';
    const en = BUILDING_KIND_EN[kind] || kind;
    const colorEn = BUILDING_COLOR_EN[kind] || '';
    // 只显示当前语言（设置面板里切换，不再中英并排）
    const stack = document.createElement('div');
    stack.className = 'bi';
    stack.textContent = L(cn + (colorCn ? ' · ' + colorCn : ''),
                          en + (colorEn ? ' · ' + colorEn : ''));
    item.appendChild(stack);
    box.appendChild(item);
  }
}

// 点位颜色图例：游戏地图上那些彩色小点分别是什么。
// 色表与编辑器 web/furniture-editor.html 的 PTYPES 完全一致（逐项比对见
// tools/_apply_and_check_points.py 的 [4] 段），所以两边看到的是同一个颜色。
function renderPointLegend() {
  const box = $('point-legend');
  if (!box) return;
  box.innerHTML = '';
  // 直接列全部 8 种：色表就是这 8 个，不依赖贴图/点位是否已加载（避免初始化时序问题）
  for (const id of Object.keys(FURN_COLORS)) {
    const item = document.createElement('div');
    item.className = 'item';
    const sw = document.createElement('span');
    sw.className = 'sw';
    sw.style.background = FURN_COLORS[id] || FURN_COLORS.idle;
    item.appendChild(sw);
    const stack = document.createElement('div');
    stack.className = 'bi';
    stack.textContent = L(POINT_CN[id] || id, POINT_EN[id] || id);
    item.appendChild(stack);
    box.appendChild(item);
  }
}

function renderLegend() {
  const box = $('legend');
  box.innerHTML = '';
  for (const name of Object.keys(MAP.legend || {})) {
    if (TERRAIN_DECOR.has(name)) continue; // 已并入草地，不在图例中列出
    const item = document.createElement('div');
    item.className = 'item';
    const sw = document.createElement('span');
    sw.className = 'sw';
    sw.style.background = TERRAIN_COLORS[name] || '#000';
    item.appendChild(sw);
    // 只显示当前语言
    const stack = document.createElement('div');
    stack.className = 'bi';
    stack.textContent = L(TERRAIN_CN[name] || name, TERRAIN_EN[name] || name);
    item.appendChild(stack);
    box.appendChild(item);
  }
}

// 建筑内的一格 → 画布像素。贴图按 S_OF[i] 以建筑中心等比放大，建筑内的格子
// 必须跟着做同一变换：否则居民（按格中心画）会与贴图里的床/家具图案错位。
// 不在任何建筑内的格子（街道、旷野）原样返回格中心。
function screenOfTile(x, y, tp) {
  const bs = (MAP && MAP.buildings) || [];
  for (let i = 0; i < bs.length; i++) {
    const b = bs[i];
    if (x >= b.x && x < b.x + b.w && y >= b.y && y < b.y + b.h) {
      const s = S_OF[i] || 1;
      if (s === 1) break;
      const bcx = (b.x + b.w / 2) * tp, bcy = (b.y + b.h / 2) * tp;
      return [bcx + ((x + 0.5) * tp - bcx) * s, bcy + ((y + 0.5) * tp - bcy) * s];
    }
  }
  return [x * tp + tp / 2, y * tp + tp / 2];
}

function drawFrame() {
  if (!staticLayer) return;
  ctx.drawImage(staticLayer, 0, 0);
  if (!STATE) return;
  const tp = MAP.tile_px;

  // 场热力叠加（bitmap 值域未定，按本帧最大值归一化，最稳）
  const ch = $('field-select').value;
  const grid = ch && STATE.field_grid && STATE.field_grid[ch];
  if (grid && grid.length) {
    let mx = 0;
    for (let i = 0; i < grid.length; i++) if (grid[i] > mx) mx = grid[i];
    if (mx > 0) {
      for (let y = 0; y < MAP.height; y++) {
        for (let x = 0; x < MAP.width; x++) {
          const v = grid[y * MAP.width + x] / mx;
          if (v > 0.05) {
            ctx.fillStyle = 'rgba(255,220,120,' + Math.min(0.75, v * 0.8) + ')';
            ctx.fillRect(x * tp, y * tp, tp, tp);
          }
        }
      }
    }
  }

  // 居民点：亮色实心 + 深色外圈 + 圆内姓名。
  // 半径统一用模块级 R_RES / RING（见文件头部），与点击容差共用，避免数值漂移。
  ctx.save();
  for (const r of STATE.residents || []) {
    const [px, py] = screenOfTile(r.x, r.y, tp);
    // 1) 深色外圈：与背景拉开对比
    ctx.beginPath();
    ctx.arc(px, py, R_RES + RING, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(10,12,16,0.85)';
    ctx.fill();
    // 2) 主色实心圆
    ctx.beginPath();
    ctx.arc(px, py, R_RES, 0, Math.PI * 2);
    ctx.fillStyle = '#ffd166';
    ctx.fill();
    if (SHOW_NAMES) {
      // 3) 姓名写进圆点内部：字号随半径自适应，保证小尺寸下两字也塞得下
      const fs = Math.max(13, Math.min(18, R_RES * 0.86));   // tile_px 24 配套：18px 两字仍塞进 42px 圆内
      ctx.font = fs + 'px "PingFang SC", "Microsoft YaHei", system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillStyle = '#2a2320';   // 黄底配深棕字，对比足够
      ctx.fillText(
        L(r.name || String(r.id), r.name_en || r.name || String(r.id)),
        px, py + 0.5);
    } else {
      // 不显示名字时保留左上高光：小尺寸下也一眼能认出是人
      ctx.beginPath();
      ctx.arc(px - R_RES * 0.3, py - R_RES * 0.3, Math.max(1, R_RES * 0.32), 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(255,255,255,0.9)';
      ctx.fill();
    }
  }
  ctx.restore();

  updateHud();
}

// —— 概览 / 事件流：统一中英双语（一行中文：数值，下一行英文翻译：数值）——
const STAT_CN = { population: '人口', mood_avg: '平均心情', buildings: '建筑' };
const STAT_EN = { population: 'Population', mood_avg: 'Avg mood', buildings: 'Buildings' };

// 快脑动作值（brain.py::ActionType）的中英标签
const ACTION_CN = {
  sleep: '睡觉', eat: '吃饭', toilet: '上厕所', work: '上班', school: '上学',
  socialize: '社交', shop: '购物', wander: '闲逛', go_home: '回家', idle: '待机',
  // 这四项原先漏了：中文界面下居民详情、地图气泡会直接蹦出 bar / fish / buy / treat。
  buy: '买渔具', fish: '钓鱼', bar: '泡吧', treat: '看病',
};
const ACTION_EN = {
  sleep: 'Sleep', eat: 'Eat', toilet: 'Toilet', work: 'Work', school: 'School',
  socialize: 'Socialize', shop: 'Shop', wander: 'Wander', go_home: 'Go home', idle: 'Idle',
  buy: 'Buy gear', fish: 'Fish', bar: 'Bar', treat: 'Doctor',
};
// 在途状态：action 在决策瞬间就写成目标动作，但人此刻还在路上（未到达目标建筑）。
// 用帧里的 moving(=路径非空) 区分「前往某地」与「已在建筑内做事」，避免「路上就显示吃饭」。
const ON_THE_WAY_CN = '在路上';
const ON_THE_WAY_EN = 'On the way';

// 由居民帧数据推导「显示用动作标签」：
// - 交互中：action 是中文交互名，原样返回；
// - 在路上（moving=true 且目标是某个建筑）：显示「在路上（前往 睡觉）」，
//   与「已在店内做事」区分开；
// - 其余：直接返回动作的中英名。
function actionLabel(r) {
  const cn = ACTION_CN[r.action] || r.action || '-';
  const en = ACTION_EN[r.action] || KIND_EN[r.action] || r.action || '-';
  const isDest = r.action && r.action !== 'wander' && r.action !== 'idle' && !KIND_EN[r.action];
  if (r.moving && isDest) {
    return L(ON_THE_WAY_CN + '（前往 ' + cn + '）', ON_THE_WAY_EN + ' to ' + en);
  }
  return L(cn, en);
}

// 关系类型（relations.py::RelKind 已是中文值）
const REL_EN = {
  陌生人: 'Stranger', 认识: 'Acquaintance', 朋友: 'Friend', 挚友: 'Best friend',
  恋人: 'Lover', 配偶: 'Spouse', 对手: 'Rival', 仇敌: 'Enemy',
};

// 交互类型（interactions.py::InteractionKind 已是中文值）的中→英
const KIND_EN = {
  打招呼: 'Greet', 聊天: 'Chat', 击掌: 'High five', 拥抱: 'Hug', 牵手: 'Hold hands',
  送礼物: 'Give gift', 邀请同行: 'Invite along', 一起吃饭: 'Eat together', 帮忙: 'Help',
  安慰: 'Comfort', 分享食物: 'Share food', 讲故事: 'Tell story', 调情: 'Flirt',
  邀约: 'Ask out', 表白: 'Confess', 求婚: 'Propose', 结婚: 'Marry', 争吵: 'Quarrel',
  辱骂: 'Insult', 推搡: 'Shove', 打架: 'Fight', 偷窃: 'Steal', 抢劫: 'Rob', 造谣: 'Spread rumor',
};

// 造一个「一行中文 / 一行英文」的双语条目
function biRow(cn, en) {
  const d = document.createElement('div');
  d.className = 'stat-bi';
  d.textContent = L(cn, en);
  return d;
}

// 分组小标题（DOM 版：供 renderStats 用 appendChild 插入）
function groupHeader(cn, en) {
  const d = document.createElement('div');
  d.className = 'stat-group';
  d.textContent = L(cn, en);
  return d;
}

// 分组小标题（字符串版：供 renderResident 拼接 html 用）。
// 注意：不能把 DOM 版直接拼进字符串，否则会渲染出 [object HTMLDivElement]。
function groupHeaderHTML(cn, en) {
  return '<div class="stat-group">' + L(cn, en) + '</div>';
}

// 最小 HTML 转义（姓名等文本进 innerHTML 前先过一遍）
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function pad2(n) { return (n < 10 ? '0' : '') + n; }

function renderStats(st) {
  const box = $('stats');
  if (!box) return;
  box.innerHTML = '';
  const fmt = (v) => (typeof v === 'number' ? (Math.round(v * 10) / 10) : v);

  // 标量字段
  for (const k of ['population', 'mood_avg', 'buildings']) {
    if (!(k in st)) continue;
    const v = fmt(st[k]);
    box.appendChild(biRow((STAT_CN[k] || k) + '：' + v, (STAT_EN[k] || k) + ': ' + v));
  }

  // 行动分布（按当前动作计数）
  const byAction = st.by_action || {};
  const actKeys = Object.keys(byAction);
  if (actKeys.length) {
    box.appendChild(groupHeader('行动分布', 'Actions'));
    for (const k of actKeys) {
      const v = byAction[k];
      // 交互进行中时 action 会被改写成中文交互名（如「打招呼」），
      // 故英文名回退到 KIND_EN，中文名直接用原值。
      const cn = ACTION_CN[k] || k;
      const en = ACTION_EN[k] || KIND_EN[k] || k;
      box.appendChild(biRow(cn + '：' + v, en + ': ' + v));
    }
  }

  // 关系图
  const rel = st.relations || {};
  box.appendChild(groupHeader('关系', 'Relations'));
  const edges = fmt(rel.edges ?? 0);
  box.appendChild(biRow('关系边数：' + edges, 'Edges: ' + edges));
  const byKind = rel.by_kind || {};
  for (const k of Object.keys(byKind)) {
    const v = byKind[k];
    box.appendChild(biRow(k + '：' + v, (REL_EN[k] || k) + ': ' + v));
  }
}

// ===== 右侧栏标签页（数据 / 日志） =====
// 左「数据」＝原有概览 / 图例 / 事件流 / 居民详情，一个不动；
// 右「日志」＝每人每天干了什么。点地图上的人 → 只看他；点「看全城」→ 回到全城。
var LOG = { tab: 'data', rid: 0, name: '', items: [], loading: false };

// 日志种类的英文说法（中文文案由后端直接给，前端只翻译不给中文，避免两处各写一套话术）
var LOG_KIND_EN = {
  work: 'on duty', school: 'at school', sleep: 'went home to sleep',
  eat: 'had a meal', socialize: 'chatting', buy: 'bought a fishing rod',
  fish: 'fishing', bar: 'at the bar', treat: 'seeing a doctor',
};

// tick 是「当天第几分钟」→ HH:MM
function logClock(tick) {
  var m = Math.max(0, (tick | 0)) % 1440;
  return pad2(Math.floor(m / 60)) + ':' + pad2(m % 60);
}

function switchSideTab(name) {
  LOG.tab = name;
  Array.prototype.forEach.call(document.querySelectorAll('.side-tab'), function (b) {
    b.classList.toggle('is-on', b.dataset.tab === name);
  });
  var d = $('side-data'), l = $('side-log'), rp = $('side-report');
  if (d) d.hidden = (name !== 'data');
  if (l) l.hidden = (name !== 'log');
  if (rp) rp.hidden = (name !== 'report');
  if (name === 'log') loadLogs();
  if (name === 'report') loadReport();
}

// ---------------------------------------------------------------------------
// 统计日报：白天 06→22 每 2 小时一段（共 8 段），夜里 22→06 合成一整段，一天 9 段。
// 数据来自 /api/report?day=N（day=0 = 正在进行的这一天）。
// 每段两个数：people = 这段时间里做过这件事的独立人数（回答「吃饭 xx 人」），
//             ticks  = 人次（每个仿真分钟累加一次，能看出这事持续了多久）。
// ---------------------------------------------------------------------------
var REPORT = { items: [], day: 0, cur: 0, loading: false };

// 日报里出现的动作名（比 ACTION_CN 多出 buy/fish/bar/treat 四项）
var REPORT_ACT_CN = {
  sleep: '睡觉', eat: '吃饭', toilet: '上厕所', work: '上班', school: '上学',
  socialize: '社交', shop: '购物', wander: '闲逛', go_home: '回家', idle: '待机',
  buy: '买渔具', fish: '钓鱼', bar: '泡吧', treat: '看病',
};
var REPORT_ACT_EN = {
  sleep: 'Sleep', eat: 'Eat', toilet: 'Toilet', work: 'Work', school: 'School',
  socialize: 'Socialize', shop: 'Shop', wander: 'Wander', go_home: 'Go home', idle: 'Idle',
  buy: 'Buy gear', fish: 'Fish', bar: 'Bar', treat: 'Doctor',
};

// 日报里动作的**固定显示顺序** —— 位置恒定，不随数值大小跳动。
// 按「一天的流程」排：上班/上学 → 生存三件套 → 自由活动 → 娱乐杂项。
// 想调整版面只改这一行数组，渲染逻辑不用动。
var REPORT_ACT_ORDER = [
  'work', 'school',
  'eat', 'toilet', 'sleep',
  'socialize', 'wander', 'shop', 'go_home',
  'bar', 'fish', 'buy', 'treat', 'idle',
];

async function loadReport() {
  if (REPORT.loading) return;
  REPORT.loading = true;
  try {
    var j = await (await fetch('/api/report?day=' + (REPORT.day || 0))).json();
    if (j && j.buckets) {
      REPORT.items = j.buckets;
      REPORT.cur = j.day || 0;
      fillReportDays(j.days || []);
    }
    renderReport();
  } catch (_) { /* 拉不到就保留现有内容 */ }
  REPORT.loading = false;
}

// 下拉：进行中那天 + 已封盘的每一天
function fillReportDays(days) {
  var sel = $('report-day');
  if (!sel) return;
  var want = [0].concat(days);
  var now = Array.prototype.map.call(sel.options || [], function (o) { return +o.value; });
  if (now.length === want.length && want.every(function (v, i) { return v === now[i]; })) return;
  sel.innerHTML = '';
  want.forEach(function (d) {
    var o = document.createElement('option');
    o.value = d;
    o.textContent = d ? T('report.day', { n: d }) : T('report.live');
    sel.appendChild(o);
  });
  sel.value = String(REPORT.day || 0);
}

function renderReport() {
  var box = $('report-body');
  if (!box) return;
  box.innerHTML = '';
  if (!REPORT.items.length) {
    box.textContent = T('report.empty');
    return;
  }
  var maxPeople = 1;
  REPORT.items.forEach(function (b) { if (b.people > maxPeople) maxPeople = b.people; });
  REPORT.items.forEach(function (b) {
    var row = document.createElement('div');
    row.className = 'rpt-bucket' + (b.night ? ' is-night' : '');
    var hd = document.createElement('div');
    hd.className = 'rpt-hd';
    var win = pad2(b.from % 24) + ':00–' + pad2(b.to % 24) + ':00';
    var winEl = document.createElement('span');
    winEl.textContent = L(win + (b.night ? '（整夜）' : ''), win + (b.night ? ' (night)' : ''));
    hd.appendChild(winEl);
    var cnt = document.createElement('span');
    cnt.className = 'rpt-people';
    cnt.textContent = L(b.people + ' 人有活动', b.people + ' active');
    hd.appendChild(cnt);
    row.appendChild(hd);

    // 固定顺序：本段没有的动作也占位（灰显），否则后面的条目会往前挪。
    var _byAct = {};
    (b.actions || []).forEach(function (a) { _byAct[a.action] = a; });
    var acts = REPORT_ACT_ORDER.map(function (k) {
      return _byAct[k] || { action: k, people: 0, ticks: 0, _zero: true };
    });
    if (!acts.length) {
      var none = document.createElement('div');
      none.className = 'rpt-none';
      none.textContent = T('report.emptyBucket');
      row.appendChild(none);
    }
    acts.forEach(function (a) {
      var line = document.createElement('div');
      line.className = 'rpt-act' + (a._zero ? ' is-zero' : '');
      var nm = document.createElement('span');
      nm.className = 'rpt-name';
      nm.textContent = L(REPORT_ACT_CN[a.action] || a.action, REPORT_ACT_EN[a.action] || a.action);
      var bar = document.createElement('span');
      bar.className = 'rpt-bar';
      var fill = document.createElement('i');
      // 条长 = 该动作的独立人数 / 本段最多的那个动作 —— 相对长度，一眼看出主次
      fill.style.width = Math.max(3, Math.round(100 * (a.people / maxPeople))) + '%';
      bar.appendChild(fill);
      var num = document.createElement('span');
      num.className = 'rpt-num';
      num.textContent = a.people + L(' 人', ' p') + ' · ' + a.ticks + L(' 人次', ' t');
      line.appendChild(nm); line.appendChild(bar); line.appendChild(num);
      row.appendChild(line);
    });
    box.appendChild(row);
  });
}

// 拉一次全量（打开日志页 / 换人时）；实时增量由 WS 帧里的 state.logs 合并进来
async function loadLogs() {
  if (LOG.loading) return;
  LOG.loading = true;
  try {
    var q = '/api/logs?limit=600' + (LOG.rid ? '&rid=' + LOG.rid : '');
    var j = await (await fetch(q)).json();
    LOG.items = (j && j.logs) ? j.logs.slice() : [];
    renderLogs();
  } catch (_) { /* 拉不到就保留现有内容 */ }
  LOG.loading = false;
}

// WS 每帧只带最近 40 条：按 id 去重后并入（帧 200ms 一次，重复率极高）
function mergeLogs(list) {
  if (!list || !list.length) return;
  var seen = {}, i, e, added = false;
  for (i = 0; i < LOG.items.length; i++) seen[LOG.items[i].id] = 1;
  for (i = 0; i < list.length; i++) {
    e = list[i];
    if (seen[e.id]) continue;
    if (LOG.rid && e.rid !== LOG.rid) continue;   // 只看某人时别把别人的塞进来
    seen[e.id] = 1;
    LOG.items.push(e);
    added = true;
  }
  if (!added) return;
  if (LOG.items.length > 800) LOG.items = LOG.items.slice(-800);
  if (LOG.tab === 'log') renderLogs();
}

function renderLogs() {
  var ul = $('log-list');
  if (!ul) return;
  ul.innerHTML = '';
  if (!LOG.items.length) {
    var em = document.createElement('li');
    em.className = 'day-sep';
    em.textContent = T('log.empty');
    ul.appendChild(em);
    return;
  }
  var curDay = null;
  for (var i = 0; i < LOG.items.length; i++) {
    var ev = LOG.items[i];
    if (ev.day !== curDay) {
      curDay = ev.day;
      var sep = document.createElement('li');
      sep.className = 'day-sep';
      sep.textContent = T('log.day', { n: curDay });
      ul.appendChild(sep);
    }
    var li = document.createElement('li');
    li.className = 'k-' + (ev.kind || 'other');
    var tm = document.createElement('span');
    tm.className = 't';
    tm.textContent = logClock(ev.tick);
    li.appendChild(tm);
    if (!LOG.rid) {                       // 全城视角才显示是谁
      var who = document.createElement('span');
      who.className = 'who';
      who.textContent = L(ev.name || ('#' + ev.rid),
        ev.name_en || enNameOf(ev.rid, ev.name) || ('#' + ev.rid));
      li.appendChild(who);
    }
    var m = document.createElement('span');
    m.className = 'm';
    m.textContent = L(ev.text || '', ev.text_en || ev.text || '');
    li.appendChild(m);
    ul.appendChild(li);
  }
  ul.scrollTop = ul.scrollHeight;         // 停在最新一条
}

// 切换「只看谁」；rid=0 表示全城
function setLogResident(rid, name, nameEn) {
  rid = rid | 0;
  var same = (LOG.rid === rid);
  LOG.rid = rid;
  LOG.name = name || '';
  LOG.nameEn = nameEn || '';
  var t = $('log-title'), all = $('log-all'), hint = $('log-hint');
  if (t) {
    t.textContent = rid
      ? L(LOG.name + ' 的日志',
          (LOG.nameEn || pinyinOf(LOG.name) || ('#' + rid)) + "'s log")
      : T('log.town');
  }
  if (all) all.classList.toggle('hidden', !rid);
  if (hint) hint.classList.toggle('hidden', !!rid);
  if (same) { if (LOG.tab === 'log') renderLogs(); return; }
  loadLogs();
}

function renderEvents(events) {
  const ul = $('events');
  if (!ul) return;
  ul.innerHTML = '';
  const nameOf = {};
  const nameEn = {};
  for (const r of STATE.residents || []) {
    nameOf[r.id] = r.name || ('#' + r.id);
    nameEn[r.id] = r.name_en || '';
  }
  // 名字按语言取：中文用本名，英文用英文名（没有则回退拼音）
  const showName = (id) => {
    const n = nameOf[id] || ('#' + id);
    return L(n, nameEn[id] || pinyinOf(n));
  };

  for (const ev of (events || []).slice().reverse()) {
    const li = document.createElement('li');
    if (ev && ev.type === 'interaction') {
      const tm = pad2(Math.floor((ev.tick || 0) / 60)) + ':' + pad2((ev.tick || 0) % 60);
      const a = showName(ev.actor);
      const b = showName(ev.target);
      const kindCn = ev.kind || T('ev.fallbackKind');
      const kindEn = (ev.kind && KIND_EN[ev.kind]) || T('ev.fallbackKind');
      li.className = 'k-social';
      li.textContent = L(tm + ' · ' + a + ' 与 ' + b + ' ' + kindCn,
                         tm + ' · ' + a + ' & ' + b + ' ' + kindEn);
    } else {
      li.textContent = (ev && ev.text) || JSON.stringify(ev);
    }
    ul.appendChild(li);
  }
}

// 顶栏运行状态徽标：每收到一帧就刷新。
// 关键作用：让用户一眼分清「运行中（绿·心跳闪）/ 已暂停（黄）」，
// 避免把「暂停」或「深夜大家都睡了、画面不动」误判成程序死机。
let _lastBeat = 0;
function updateRunState() {
  const el = $('run-state');
  if (!el) return;
  const paused = !!(STATE && STATE.paused);
  el.classList.toggle('is-paused', paused);
  el.classList.remove('beat');
  if (paused) {
    el.textContent = T('run.paused');
    el.title = T('run.pausedTitle');
  } else {
    el.textContent = T('run.running');
    el.title = T('run.runningTitle');
    // 心跳：帧间隔足够久才闪一次，避免高频闪烁刺眼
    const now = Date.now();
    if (now - _lastBeat > 900) {
      _lastBeat = now;
      void el.offsetWidth;          // 强制重排，让动画可重复触发
      el.classList.add('beat');
      setTimeout(() => el.classList.remove('beat'), 350);
    }
  }
}

// 速度滑条 ↔ 倍率：滑块 -10..10，**中点 0 就是 1×**；对数刻度，左端 0.1×、右端 10×。
// 用对数是因为线性档在 1 附近挪一格就翻倍（1→2→3），手太糙；对数下 1 附近一格只差 10%。
function sliderToSpeed(v) { return Math.pow(10, (+v || 0) / 10); }
function speedToSlider(sp) {
  const s = (sp === undefined || sp === null) ? 1 : +sp;
  if (!(s > 0)) return 10;                       // ∞（无限倍速）当成最大档显示
  return Math.max(-10, Math.min(10, Math.round(10 * Math.log10(s) * 2) / 2));
}
function fmtSpeed(sp) {
  const s = (sp === undefined || sp === null) ? 1 : +sp;
  if (!(s > 0)) return '∞';
  if (Math.abs(s - 1) < 0.05) return '1×';        // 别显示成「1.00×」，常速就该是 1×
  if (s < 1) return parseFloat(s.toFixed(2)) + '×';
  if (s < 10) return parseFloat(s.toFixed(1)) + '×';
  return Math.round(s) + '×';
}

function updateHud() {
  if (!STATE) return;
  updateRunState();
  const c = STATE.clock || {};
  $('clock').textContent = c.clock || '--:--';
  $('day').textContent = T('hud.day', { n: (c.day ?? 1) });
  $('night').classList.toggle('hidden', !c.is_night);
  $('btn-pause').textContent = STATE.paused ? T('top.resume') : T('top.pause');
  // speed<=0 是「无限倍速」，滑条已不含 0 档；若外部把它设回 0（如 config.yaml），
  // 这里显式标成 ∞ 并提示，避免用户以为「人卡住了」。
  const sp = STATE.speed ?? 1;
  $('speed-val').textContent = fmtSpeed(sp);
  const _sl = $('speed');
  if (_sl) _sl.value = speedToSlider(sp);        // 后端是真相：切档 / 重置后回填滑条位置

  renderStats(STATE.stats || {});
  renderEvents(STATE.events || []);
  refreshResident();
}

// 居民面板随帧实时刷新：动作 / 需求 / 心情都在变，不能只在点开那一刻渲染一次
function refreshResident() {
  const panel = $('resident-panel');
  if (!panel || panel.hidden || _lastRid == null) return;
  const r = (STATE.residents || []).find((p) => p.id === _lastRid);
  if (r) renderResident(r);
}

// 语言切换后重绘所有由 JS 生成的动态内容（静态文案由 i18n.applyStatic 负责）。
function relocalize() {
  renderLegend();
  renderBuildingLegend();
  renderPointLegend();
  if (MAP) {
    const sel = $('field-select');
    if (sel && sel.value) loadField(sel.value);
  }
  if (STATE) {
    updateRunState();
    updateHud();          // 内含 renderStats / renderEvents / refreshResident
  }
  // 日志页的标题 / 「看全城」按钮 / 顶部提示是在 setLogResident() 里写的，
  // 切语言时没人重新调它 —— 这就是「切了英文，日志页标题还是中文」的来源。
  // 传入同一个 rid 时它走内部 same 分支，只重绘 DOM 不发请求，所以放这儿是安全的。
  try { setLogResident(LOG.rid, LOG.name, LOG.nameEn || ''); } catch (_) {}
  // 日报和日志页是「按当前语言重建 DOM」的，切语言必须重画一遍，
  // 否则得手动刷新页面才变英文。两者都读本地缓存，不会重新打接口。
  try { renderLogs(); } catch (_) {}
  try { renderReport(); } catch (_) {}
  const rday = $('report-day');
  if (rday) Array.prototype.forEach.call(rday.options || [], (o) => {
    const d = +o.value;
    o.textContent = d ? T('report.day', { n: d }) : T('report.live');
  });
  if (_lastBid != null && MAP) {
    const b = (MAP.buildings || []).find((bb) => bb.bid === _lastBid);
    if (b) renderBuilding(b);
  }
  if (_lastRid != null) refreshResident();
  // 顶栏「已连接」是 WS onopen 那一刻写死的，切语言没人重写它。
  const st = $('status');
  if (st && WS && WS.readyState === 1) st.textContent = T('status.connected');
  // 鼠标正悬停时，气泡里还是旧语言的句子 —— 直接收掉，比原地改字更干净
  hideTooltip();
}

let FIELD_GRID = null;

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  WS = new WebSocket(proto + '://' + location.host + '/ws');
  WS.onopen = () => {
    $('status').textContent = T('status.connected');
    const el = $('run-state');
    if (el && !STATE) { el.textContent = T('run.connected'); el.classList.remove('is-paused'); }
  };
  WS.onclose = () => {
    $('status').textContent = T('status.lost');
    const el = $('run-state');
    if (el) { el.textContent = T('run.lost'); el.classList.add('is-paused'); }
    setTimeout(connect, 1500);
  };
  WS.onmessage = (e) => {
    try { STATE = JSON.parse(e.data); } catch (_) { return; }
    populateFields();
    if (FIELD_GRID) STATE.field_grid = FIELD_GRID;
    mergeLogs(STATE.logs);          // 日志增量（按 id 去重合并）
    drawFrame();
  };
}

function populateFields() {
  const sel = $('field-select');
  if (sel.dataset.ready || !STATE || !STATE.fields) return;
  for (const name of Object.keys(STATE.fields)) {
    const o = document.createElement('option');
    o.value = name; o.textContent = name;
    sel.appendChild(o);
  }
  sel.dataset.ready = '1';
}

async function loadField(ch) {
  const peak = $('field-peak');
  if (!ch) { FIELD_GRID = null; peak.textContent = ''; drawFrame(); return; }
  let pk = '';
  try {
    const r = await fetch('/api/fields/' + encodeURIComponent(ch));
    const j = await r.json();
    FIELD_GRID = { [ch]: j.bitmap || [] };
    pk = T('field.peak', {
      peak: (j.peak != null ? (+j.peak).toFixed(3) : '-'),
      live: (j.live ?? '-'),
    });
  } catch (_) { FIELD_GRID = null; pk = ''; }
  peak.textContent = pk;
  drawFrame();
}

async function control(action, value) {
  const body = { action };
  if (value !== undefined) body.value = value;
  await fetch('/api/control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

function showTooltip(ev) {
  if (!MAP) return;
  const rect = canvas.getBoundingClientRect();
  const scale = canvas.width / rect.width;
  const x = Math.floor((ev.clientX - rect.left) * scale / MAP.tile_px);
  const y = Math.floor((ev.clientY - rect.top) * scale / MAP.tile_px);
  if (x < 0 || y < 0 || x >= MAP.width || y >= MAP.height) return hideTooltip();
  const t = Object.keys(MAP.legend).find((k) => MAP.legend[k] === tileAt(x, y)) || '?';
  let text = T('tool.coord', { x: x, y: y }) +
             L(TERRAIN_CN[t] || t, TERRAIN_EN[t] || t);
  const b = (MAP.buildings || []).find(
    (bb) => x >= bb.x && x < bb.x + bb.w && y >= bb.y && y < bb.y + bb.h);
  if (b) text += ' · ' + L(b.label || BUILDING_KIND_CN[b.kind] || b.kind,
                           BUILDING_KIND_EN[b.kind] || b.kind);
  const r = (STATE && STATE.residents || []).find((p) => p.x === x && p.y === y);
  if (r) {
    text += ' · ' + L(r.name || r.id, r.name_en || pinyinOf(r.name) || String(r.id)) +
            ' · ' + actionLabel(r);
  }
  tooltip.textContent = text;
  tooltip.style.left = (ev.clientX - rect.left + 12) + 'px';
  tooltip.style.top = (ev.clientY - rect.top + 12) + 'px';
  tooltip.classList.remove('hidden');
}
function hideTooltip() { tooltip.classList.add('hidden'); }
let _tiles = null;
function tileAt(x, y) { return _tiles ? _tiles[y * MAP.width + x] : -1; }

// —— 居民详情（/api/resident/{rid}）：点击地图上的居民点，右侧面板出文本详情 ——
// 需求呈现：饱食度（高=饱、低=饿）。后端 need 里仍带内部 hunger 键，渲染时跳过。
const NEED_CN = { satiety: '饱食度', bladder: '内急', energy: '精力', social: '社交' };
const NEED_EN = { satiety: 'Satiety', bladder: 'Bladder', energy: 'Energy', social: 'Social' };
// 注意：后端 ocean 字典用的是 dataclass 字段全名（resident.py::Ocean）
const OCEAN_CN = {
  openness: '开放性', conscientiousness: '尽责性', extraversion: '外向性',
  agreeableness: '宜人性', neuroticism: '神经质', greed: '财迷',
  o: '开放性', c: '尽责性', e: '外向性', a: '宜人性', n: '神经质',
};
const OCEAN_EN = {
  openness: 'Openness', conscientiousness: 'Conscientiousness', extraversion: 'Extraversion',
  agreeableness: 'Agreeableness', neuroticism: 'Neuroticism', greed: 'Greed',
  o: 'Openness', c: 'Conscientiousness', e: 'Extraversion', a: 'Agreeableness', n: 'Neuroticism',
};
// 职业（resident.py::Job 的英文枚举值 → 中文）
const JOB_CN = {
  unemployed: '无业', farmer: '农民', worker: '工人', shopkeeper: '店主',
  teacher: '教师', student: '学生', child: '儿童', doctor: '医生',
};
const JOB_EN = {
  unemployed: 'Unemployed', farmer: 'Farmer', worker: 'Worker', shopkeeper: 'Shopkeeper',
  teacher: 'Teacher', student: 'Student', child: 'Child', doctor: 'Doctor',
};
const SEX_EN = { M: 'Male', F: 'Female' };

// —— 姓名音译 ——
// 后端姓名 = 中文姓 + 中文名（resident.py::SURNAMES / GIVEN_M / GIVEN_F）。
// 这里把每个字映射到拼音，事件流 / 居民面板的英文行用人名拼音呈现。
const PY = {
  赵: 'Zhao', 钱: 'Qian', 孙: 'Sun', 李: 'Li', 周: 'Zhou', 吴: 'Wu', 郑: 'Zheng', 王: 'Wang',
  冯: 'Feng', 陈: 'Chen', 褚: 'Chu', 卫: 'Wei', 蒋: 'Jiang', 沈: 'Shen', 韩: 'Han', 杨: 'Yang',
  朱: 'Zhu', 秦: 'Qin', 尤: 'You', 许: 'Xu', 何: 'He', 吕: 'Lyu', 施: 'Shi', 张: 'Zhang',
  孔: 'Kong', 曹: 'Cao', 严: 'Yan', 华: 'Hua', 金: 'Jin', 魏: 'Wei', 陶: 'Tao', 姜: 'Jiang',
  强: 'Qiang', 伟: 'Wei', 军: 'Jun', 磊: 'Lei', 勇: 'Yong', 涛: 'Tao', 明: 'Ming', 超: 'Chao',
  刚: 'Gang', 平: 'Ping', 辉: 'Hui', 斌: 'Bin',
  芳: 'Fang', 静: 'Jing', 敏: 'Min', 燕: 'Yan', 丽: 'Li', 娟: 'Juan', 霞: 'Xia', 艳: 'Yan',
  玲: 'Ling', 萍: 'Ping', 婷: 'Ting', 雪: 'Xue',
};

// 姓名 -> 拼音（「张三」→「Zhang San」）；未知字原样保留
// 名字的英文侧：优先用后端带出的 name_en（只取名、不带姓），
// 拿不到（老日志 / 老库 / 老快照）就回退拼音 —— 绝不让中文名出现在英文界面里。
// 缓存跟着 STATE.residents 的数组引用走：换了一批人（重置仿真）会自动重建。
let _nameEnCache = null, _nameEnSrc = null;
function enNameOf(id, cn) {
  const arr = STATE.residents || [];
  if (_nameEnSrc !== arr) {
    _nameEnSrc = arr;
    _nameEnCache = {};
    for (const r of arr) if (r && r.id != null) _nameEnCache[r.id] = r.name_en || '';
  }
  return _nameEnCache[id] || (cn ? pinyinOf(cn) : '');
}

function pinyinOf(name) {
  if (!name) return '';
  let out = '';
  for (const ch of String(name)) out += (PY[ch] ? PY[ch] + ' ' : ch);
  return out.trim();
}

// 居民档案行：键「中文 + 英文小字」；值也支持英文小字（valEn）
function kvRow(label, val, en, valEn) {
  const key = '<span>' + esc(L(label, en || label)) + '</span>';
  // valEn 非空 → 值也按语言二选一（如「男 · 20 岁」/「Male · 20y」）
  const v = (valEn != null && valEn !== '')
    ? esc(String(L(String(val), String(valEn))))
    : val;
  return '<div class="stat-row"><span class="k">' + key + '</span><b class="mono">' + v + '</b></div>';
}

function tileFromEvent(ev) {
  const rect = canvas.getBoundingClientRect();
  const scale = canvas.width / rect.width;
  return {
    x: Math.floor((ev.clientX - rect.left) * scale / MAP.tile_px),
    y: Math.floor((ev.clientY - rect.top) * scale / MAP.tile_px),
  };
}

function residentAt(x, y) {
  const rs = (STATE && STATE.residents) || [];
  const tp = MAP ? MAP.tile_px : 6;
  // 优先：精确落在同一格
  const exact = rs.find((p) => p.x === x && p.y === y);
  if (exact) return exact;
  // 兜底：点大之后「点在点上」的宽容判定——取像素距离最近、且在点击半径内的那个。
  // 半径与绘制半径同源（R_RES + 外圈 + 4px 余量），解决「点不中」的问题。
  const [cx, cy] = screenOfTile(x, y, tp);
  const r = R_RES + RING + 4;
  let best = null, bestD = r * r;
  for (const p of rs) {
    const [ppx, ppy] = screenOfTile(p.x, p.y, tp);
    const dx = ppx - cx, dy = ppy - cy;
    const d = dx * dx + dy * dy;
    if (d <= bestD) { best = p; bestD = d; }
  }
  return best;
}

function renderResident(r) {
  $('resident-panel').hidden = false;
  const nm = r.name || ('#' + r.id);
  // 标题里的名字：中文名 + 拼音小字
  $('r-name').textContent = L(nm, r.name_en || pinyinOf(nm) || nm);
  const nb = r.need || {}, oc = r.ocean || {};
  let html = '';
  const sexCn = r.sex === 'M' ? '男' : '女';
  const sexEn = SEX_EN[r.sex] || r.sex;
  // 当前动作：帧里可能是英文枚举（toilet / eat / sleep…），
  // 也可能是交互进行中的中文交互名（打招呼 / 聊天…），两种都要中英对照；
  // 在途时显示「在路上（前往 X）」，与「已在店内做事」区分。
  const act = actionLabel(r);
  html += kvRow('编号', '#' + r.id, 'No.', '');
  html += kvRow('性别 / 年龄', sexCn + ' · ' + r.age + ' 岁', 'Sex / Age',
                sexEn + ' · ' + r.age + 'y');
  html += kvRow('职业', (JOB_CN[r.job] || r.job) || '-', 'Job', JOB_EN[r.job] || '');
  html += kvRow('位置', '(' + r.x + ',' + r.y + ')', 'Position', '');
  html += kvRow('当前动作', act, 'Action', '');
  html += kvRow('心情', r.mood ?? '-', 'Mood', '');
  // 财务压力：独立于心情的一条线（缺钱的底噪 + 花钱的肉痛），
  // 只抬「上班」的吸引力，绝不回头扣心情 —— 否则花钱→压力→去酒吧→又花钱的死循环。
  if (r.fin_stress != null) {
    html += kvRow('财务压力', Math.round(r.fin_stress), 'Financial stress',
                  String(Math.round(r.fin_stress)));
  }
  if (r.health != null) html += kvRow('健康', Math.round(r.health), 'Health', '');
  if (r.sick_days > 0) {
    html += kvRow('状态', '生病（还需 ' + r.sick_days + ' 天）', 'Status',
                  'Sick (' + r.sick_days + 'd left)');
  }
  html += groupHeaderHTML('需求', 'Needs');
  for (const k of Object.keys(nb)) {
    const NEED_MORE_CN = { fatigue: '劳累', stress: '压力' };
    if (k === 'hunger') continue;   // 内部字段：对外只显示「饱食度 satiety」
    html += kvRow('　' + (NEED_CN[k] || NEED_MORE_CN[k] || k), nb[k], NEED_EN[k] || k);
  }
  // —— 钱包 / 职业 / 最近收支（货币系统）：让界面上看得见谁有钱、谁欠债、钱花在哪 ——
  html += groupHeaderHTML('钱包', 'Wallet');
  html += kvRow('　现金', (r.money < 0 ? '' : '') + Math.round(r.money),
                'Money', String(Math.round(r.money)));
  if (r.debt) html += kvRow('　状态', '负债', 'Status', 'In debt');
  html += kvRow('　职业', JOB_CN[r.job] || r.job, 'Job', JOB_EN[r.job] || r.job);
  if (r.work_id != null) {
    const tile = r.job_tile ? (' (' + r.job_tile[0] + ',' + r.job_tile[1] + ')') : '';
    html += kvRow('　工位', '#' + r.work_id + tile, 'Work post', '#' + r.work_id);
  }
  // 学识 / 经验：影响求职打分（能去哪类岗位）与谈薪倍率（同样工时挣多少）
  if (r.education != null) {
    const edu = Math.round(r.education * 100) + '%';
    html += kvRow('　学识', edu, 'Education', edu);
  }
  if (r.exp_total != null) {
    html += kvRow('　经验', Math.round(r.exp_total) + ' 小时', 'Experience',
                  Math.round(r.exp_total) + ' h');
  }
  const lg = r.ledger || [];
  if (lg.length) {
    html += groupHeaderHTML('最近收支', 'Recent ledger');
    for (const it of lg) {                 // [仿真日, 金额, 项目名]，已由后端倒序
      const amt = Math.round(it[1]);
      html += kvRow('　第' + it[0] + '天 · ' + it[2], (amt > 0 ? '+' : '') + amt,
                    'Day ' + it[0] + ' · ' + (it[3] || it[2]), '');
    }
  }
  html += groupHeaderHTML('性格 OCEAN', 'Personality OCEAN');
  for (const k of Object.keys(oc)) html += kvRow('　' + (OCEAN_CN[k] || k), oc[k], OCEAN_EN[k] || k);
  $('r-body').innerHTML = html;
}

// ---- 建筑营业时间：点击地图上的建筑 → 弹面板设置 opens/closes ----
// 命中判定：从上往下找第一个包含该 tile 的建筑（后画的在上层，优先命中）。
function buildingAt(x, y) {
  if (!MAP) return null;
  const bs = MAP.buildings || [];
  for (let i = bs.length - 1; i >= 0; i--) {
    const b = bs[i];
    if (x >= b.x && x < b.x + b.w && y >= b.y && y < b.y + b.h) return b;
  }
  return null;
}

// 小时数 → HH:MM（如 6 → 06:00，21.5 → 21:30）
function fmtHour(v) {
  if (v == null || isNaN(v)) return '--:--';
  const h = Math.floor(v), m = Math.round((v - h) * 60);
  return String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0');
}

let _lastBid = null;

function renderBuilding(b) {
  $('building-panel').hidden = false;
  $('b-name').textContent = L(BUILDING_KIND_CN[b.kind] || b.kind,
                              BUILDING_KIND_EN[b.kind] || b.kind);
  let html = '';
  html += kvRow('编号', '#' + b.bid, 'No.', '');
  html += kvRow('位置', '(' + b.x + ',' + b.y + ') · ' + b.w + '×' + b.h, 'Position', '');
  html += kvRow('当前营业', fmtHour(b.opens) + ' – ' + fmtHour(b.closes), 'Hours', '');
  $('b-body').innerHTML = html;
  $('b-opens').value = b.opens;
  $('b-closes').value = b.closes;
  const fullDay = (b.opens === b.closes) || (b.opens <= 0 && b.closes >= 24);
  $('b-note').textContent = fullDay
    ? T('b.fullDay')
    : (b.opens < b.closes
        ? T('b.openHours', { a: fmtHour(b.opens), b: fmtHour(b.closes) })
        : T('b.crossNight', { a: fmtHour(b.opens), b: fmtHour(b.closes) }));
  $('b-msg').textContent = '';
}

function showBuilding(ev) {
  if (!MAP) return;
  const { x, y } = tileFromEvent(ev);
  const hit = buildingAt(x, y);
  if (!hit) return;              // 点在空地上：保持原面板
  _lastBid = hit.bid;
  renderBuilding(hit);
}

async function saveBuildingHours() {
  if (_lastBid == null) return;
  const msg = $('b-msg');
  const opens = parseFloat($('b-opens').value);
  const closes = parseFloat($('b-closes').value);
  if (isNaN(opens) || isNaN(closes) || opens < 0 || opens > 24 || closes < 0 || closes > 24) {
    msg.textContent = T('b.badInput'); msg.style.color = '#e06c6c'; return;
  }
  try {
    const res = await fetch('/api/building/' + _lastBid + '/hours', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ opens: opens, closes: closes }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.error) throw new Error(data.error || ('HTTP ' + res.status));
    const b = (MAP.buildings || []).find((o) => o.bid === _lastBid);
    if (b) { b.opens = opens; b.closes = closes; }
    msg.textContent = T('b.saved'); msg.style.color = '#5fbf7a';
    if (b) renderBuilding(b);
  } catch (e) {
    msg.textContent = T('b.saveFailed', { msg: e.message }); msg.style.color = '#e06c6c';
  }
}

let _lastRid = null;

async function showResident(ev) {
  if (!MAP) return;
  const { x, y } = tileFromEvent(ev);
  const hit = residentAt(x, y);
  if (!hit) return;
  // 同一个居民且面板仍开着 → 不重复拉取详情（动作更新交给每帧 refreshResident）
  if (hit.id === _lastRid && !$('resident-panel').hidden) return;
  _lastRid = hit.id;
  setLogResident(hit.id, hit.name, hit.name_en || '');   // 日志页跟随：点谁就看谁的日志
  renderResident(hit);                       // 先用帧里的数据秒出，再拉完整详情覆盖
  try {
    const full = await (await fetch('/api/resident/' + hit.id)).json();
    if (full && !full.error) renderResident(full);
  } catch (_) { /* 拉失败就保留帧数据 */ }
}

async function boot() {
  MAP = await (await fetch('/api/map')).json();
  _tiles = b64ToBytes(MAP.tiles);
  for (const [name, val] of Object.entries(MAP.legend || {})) NAME_BY_VALUE[val] = name;
  drawStatic();
  renderLegend();
  loadMapDecor(); // 贴图与点位异步加载，加载完自动重绘地图
  renderBuildingLegend();
  renderPointLegend();

  $('btn-pause').onclick = () => control(STATE && STATE.paused ? 'resume' : 'pause');
  $('btn-step').onclick = () => control('step', 1);
  $('btn-reset').onclick = () => control('reset');
  $('speed').oninput = (e) => {
    const s = sliderToSpeed(e.target.value);
    $('speed-val').textContent = fmtSpeed(s);
    control('speed', s);
  };
  // 「1×」复位：从无限/极快一键回到正常速度，避免滑条拖到底（0=无限）后再也看不清人怎么动
  const spReset = $('speed-reset');
  if (spReset) spReset.onclick = () => {
    $('speed').value = 0;
    $('speed-val').textContent = '1×';
    control('speed', 1);
  };
  $('field-select').onchange = (e) => loadField(e.target.value);
  const namesCb = $('show-names');
  if (namesCb) {
    SHOW_NAMES = namesCb.checked;
    namesCb.onchange = (e) => { SHOW_NAMES = e.target.checked; drawFrame(); };
  }
  // 鼠标移动：拖拽中只平移，不弹提示、不选人（否则一拖地图就疯狂弹窗）
  canvas.onmousemove = (e) => {
    if (PAN && (PAN.moved || panMoved(e))) { hideTooltip(); return; }
    showTooltip(e); showResident(e);
  };
  canvas.onmouseleave = hideTooltip;
  $('r-close').onclick = () => {
    $('resident-panel').hidden = true;
    _lastRid = null;
    setLogResident(0, '');       // 关掉居民面板 → 日志页回到全城
  };
  // 右侧栏标签页：数据 / 日志
  Array.prototype.forEach.call(document.querySelectorAll('.side-tab'), (b) => {
    b.onclick = () => switchSideTab(b.dataset.tab);
  });
  if ($('log-all')) $('log-all').onclick = () => setLogResident(0, '');
  if ($('r-log')) $('r-log').onclick = () => {
    if (_lastRid == null) return;
    const rr = (STATE.residents || []).find((x) => x.id === _lastRid);
    setLogResident(_lastRid, rr ? rr.name : '', rr ? (rr.name_en || '') : '');
    switchSideTab('log');
  };
  // 按住鼠标拖动 = 平移地图（放大后最顺手的移动方式）；中键同样可用，右键留给浏览器菜单
  canvas.addEventListener('mousedown', panStart);
  window.addEventListener('mousemove', panMove);
  window.addEventListener('mouseup', panEnd);
  window.addEventListener('blur', panEnd);       // 切走窗口别卡在拖拽态
  // 点击建筑 → 打开营业时间设置面板（刚拖完地图的那一下不算点击）
  canvas.onclick = (e) => {
    if (performance.now() - _panEndAt < 300) return;
    showBuilding(e);
  };
  $('b-close').onclick = () => { $('building-panel').hidden = true; _lastBid = null; };
  $('b-save').onclick = saveBuildingHours;
  // 地图缩放：－/＋ 以舞台中心为锚点，滚轮以鼠标为锚点，双向都能走。
  $('zoom-in').onclick = () => zoomAt(1.25);
  $('zoom-out').onclick = () => zoomAt(1 / 1.25);
  $('zoom-fit').onclick = fitZoom;
  // 滚轮直接缩放（不必先点 ＋）：打开即全貌 → 想看清谁是谁再放大。
  canvas.addEventListener('wheel', (ev) => {
    if (!MAP) return;
    ev.preventDefault();                 // 只缩放地图，别让页面跟着滚
    // 滚轮缩放：按滚动量连续变化（1.0008^|deltaY| ≈ 鼠标一格 +8%），触控板小步更细。
    // 原来固定 ×1.2（一格 +20%），用户反馈「一滚就飞，找不到要看的东西」。
    zoomAt(Math.pow(1.0008, -ev.deltaY), ev.clientX, ev.clientY);
  }, { passive: false });
  // 双击画布 = 回到全貌（缩到角落时不用再去顶栏找「适应」）
  canvas.addEventListener('dblclick', () => fitZoom());
  // 窗口尺寸变化：没手动缩放过 → 重新铺成整图 100%；手动过 → 只刷新基准与百分比
  window.addEventListener('resize', () => {
    if (!MAP) return;
    if (ZOOM_MANUAL) { syncZoomRange(); setZoom(ZOOM); }
    else fitZoom();
  });
  // 首屏就是整张地图（旧实现是 applyZoom() = 100%，3840×2560 必然溢出成左上角一局部）
  fitZoom();

  // —— 右上角设置面板：语言切换（立即生效，localStorage 记忆）——
  const langSel = $('lang-select');
  if (langSel) {
    langSel.innerHTML = '';
    window.i18n.LANGS.forEach((lg) => {
      const o = document.createElement('option');
      o.value = lg.code;
      o.textContent = L(lg.name, lg.name_en || lg.name);
      langSel.appendChild(o);
    });
    langSel.value = window.i18n.getLang();
    langSel.onchange = (e) => window.i18n.setLang(e.target.value);
  }
  const pop = $('settings-pop');
  const btnSet = $('btn-settings');
  if (btnSet) btnSet.onclick = (ev) => {
    ev.stopPropagation();
    if (pop) pop.classList.toggle('hidden');
  };
  if ($('settings-close')) {
    $('settings-close').onclick = () => { if (pop) pop.classList.add('hidden'); };
  }
  // 点面板外任意处收起（画布 / 顶栏都可以）
  document.addEventListener('click', (ev) => {
    if (!pop || pop.classList.contains('hidden')) return;
    if (pop.contains(ev.target) || (btnSet && btnSet.contains(ev.target))) return;
    pop.classList.add('hidden');
  });
  window.i18n.onLang(relocalize);

  // 页面每次加载自动恢复运行：paused 状态存在服务端，上次点过暂停会导致
  // 「以后每次打开都是一屏静止的人」，很难分清是死机还是暂停。
  // 这里加载即清暂停；想静止观察随时点「暂停」，顶栏徽标会同步显示 ▶/⏸。
  try { await control('resume'); } catch (_) { /* 服务未就绪则忽略，WS 会推送真实状态 */ }

  // 顶栏「⏸ 已暂停」徽标可直接点击恢复运行。
  // 只在「已暂停」时生效——运行中点击不做事，避免把状态徽标变成误触陷阱。
  const rsEl = $('run-state');
  if (rsEl) {
    rsEl.style.cursor = 'pointer';
    rsEl.onclick = () => {
      if (STATE && STATE.paused) {
        rsEl.title = T('run.resuming');
        control('resume');
      }
    };
  }

  connect();
}

boot().catch((e) => { $('status').textContent = T('status.initFailed', { msg: e.message }); });
