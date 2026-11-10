// EvilTown 全局语言（i18n）
// ------------------------------------------------------------
// 设计：整站只显示一种语言（不再中英并排）。
//   - t(key, vars)：取字典文案，{name} 占位符做替换；
//   - L(zh, en)   ：按当前语言二选一，en 为空时回落中文；
//   - setLang(code)：切换 + 写入 localStorage + 刷新静态文案 + 通知监听者；
//   - 静态 HTML 用 data-i18n / data-i18n-title 标记，动态内容由 app.js 的
//     relocalize() 重绘。
// 语言选择存 localStorage('eviltown.lang')，刷新不丢。
'use strict';
(function (global) {
  var KEY = 'eviltown.lang';
  var LANGS = [
    { code: 'zh', name: '中文' },
    { code: 'en', name: 'English' }
  ];

  var DICT = {
    zh: {
      'app.title': 'EvilTown · 小镇模拟器',

      'top.pause': '暂停',
      'top.resume': '继续',
      'top.step': '单步',
      'top.reset': '重置',
      'top.speed': '速度',
      'top.speedTitle': '仿真速度：滑条中点 1× = 常速（约 5 分钟一天）；往左放慢到 0.1×（一天约 48 分钟，适合看细节），往右加速到 10×。',
      'top.speedReset': '恢复正常速度 1×',
      'top.names': '名字',
      'top.namesTitle': '在地图上的居民点内显示姓名',
      'top.zoomTitle': '地图缩放（人和建筑一起放大，便于点选）',
      'top.zoomOut': '缩小地图',
      'top.zoomIn': '放大地图',
      'top.zoomFit': '缩放到适应窗口',
      'top.fit': '适应',
      'top.settings': '设置',

      'panel.overview': '概览',
      'panel.buildingLegend': '建筑图例',
      'panel.pointLegend': '点位颜色',
      'panel.field': '场热力',
      'panel.events': '事件流',
      'panel.resident': '居民',
      'panel.building': '建筑',

      'field.off': '关闭',
      'field.hint': '选择通道后叠加显示 CA 场浓度（越亮越浓）',
      'field.peak': '峰值 {peak} · 活跃 {live}',

      'b.opens': '开门',
      'b.closes': '关门',
      'b.save': '保存',
      'b.fullDay': '全天开放（开门与关门相同，或 0–24）',
      'b.openHours': '营业时段 {a}–{b}',
      'b.crossNight': '跨夜时段 {a}–次日 {b}',
      'b.badInput': '请输入 0–24 之间的时间',
      'b.saveFailed': '保存失败：{msg}',
      'b.saved': '已保存，立即生效 ✓',

      'settings.title': '设置',
      'settings.language': '语言 / Language',
      'settings.hint': '点一下马上换，不用刷新；下次进来还记得你选的。',
      'settings.close': '关闭',

      'status.connecting': '连接中…',
      'status.connected': '已连接',
      'status.lost': '连接断开，重试中…',
      'status.initFailed': '初始化失败：{msg}',

      'run.connecting': '连接中…',
      'run.connected': '● 已连接',
      'run.lost': '○ 连接断开',
      'run.paused': '⏸ 已暂停 · 点击恢复',
      'run.pausedTitle': '仿真已暂停（服务端状态）。点这里或顶部「继续」即可恢复运行。',
      'run.running': '▶ 运行中',
      'run.runningTitle': '仿真正常运行中（绿点闪动 = 正在推帧）。',
      'run.resuming': '正在恢复…',

      'hud.day': '第 {n} 天',
      'hud.night': '夜',

      'tool.coord': '（{x},{y}）',
      // 右侧栏标签页 + 日志页
      'tab.data': '数据',
      'tab.log': '日志',
      'tab.report': '日报',
      'report.title': '统计日报',
      'report.hint': '白天每 2 小时一段，夜里 22:00–06:00 合成一整段',
      'report.live': '进行中',
      'report.day': '第 {n} 天',
      'report.empty': '还没有统计数据，跑一会儿就有了。',
      'report.emptyBucket': '这段时间没什么活动',
      'log.town': '小镇日志',
      'log.all': '看全城',
      'log.hint': '点地图上的人，只看他这一天干了什么',
      'log.empty': '还没有日志，跑一会儿就有了。',
      'log.day': '第 {n} 天',
      'r.log': '日志',
      'ev.fallbackKind': '互动'
    },

    en: {
      'app.title': 'EvilTown · Town Simulator',

      'top.pause': 'Pause',
      'top.resume': 'Resume',
      'top.step': 'Step',
      'top.reset': 'Reset',
      'top.speed': 'Speed',
      'top.speedTitle': 'Simulation speed: slider centre = 1× (about 5 min per day); slide left down to 0.1× (about 48 min per day, good for details), right up to 10×.',
      'top.speedReset': 'Back to normal speed (1×)',
      'top.names': 'Names',
      'top.namesTitle': 'Show resident names inside their dots on the map',
      'top.zoomTitle': 'Map zoom (residents and buildings scale together for easier picking)',
      'top.zoomOut': 'Zoom out',
      'top.zoomIn': 'Zoom in',
      'top.zoomFit': 'Fit to window',
      'top.fit': 'Fit',
      'top.settings': 'Settings',

      'panel.overview': 'Overview',
      'panel.buildingLegend': 'Building legend',
      'panel.pointLegend': 'Point colors',
      'panel.field': 'Field heatmap',
      'panel.events': 'Event stream',
      'panel.resident': 'Resident',
      'panel.building': 'Building',

      'field.off': 'Off',
      'field.hint': 'Pick a channel to overlay its CA field concentration (brighter = denser)',
      'field.peak': 'Peak {peak} · live {live}',

      'b.opens': 'Opens',
      'b.closes': 'Closes',
      'b.save': 'Save',
      'b.fullDay': 'Open all day (opens equals closes, or 0–24)',
      'b.openHours': 'Open {a}–{b}',
      'b.crossNight': 'Overnight {a}–next day {b}',
      'b.badInput': 'Please enter a time between 0 and 24',
      'b.saveFailed': 'Save failed: {msg}',
      'b.saved': 'Saved ✓',

      'settings.title': 'Settings',
      'settings.language': 'Language / 语言',
      'settings.hint': 'Switches instantly, no refresh needed. Remembered next time.',
      'settings.close': 'Close',

      'status.connecting': 'Connecting…',
      'status.connected': 'Connected',
      'status.lost': 'Disconnected, retrying…',
      'status.initFailed': 'Init failed: {msg}',

      'run.connecting': 'Connecting…',
      'run.connected': '● Connected',
      'run.lost': '○ Disconnected',
      'run.paused': '⏸ Paused · click to resume',
      'run.pausedTitle': 'Simulation is paused (server state). Click here or the Resume button to continue.',
      'run.running': '▶ Running',
      'run.runningTitle': 'Simulation is running (the green dot blinks as frames arrive).',
      'run.resuming': 'Resuming…',

      'hud.day': 'Day {n}',
      'hud.night': 'Night',

      'tool.coord': '({x},{y}) ',
      // Side panel tabs + log page
      'tab.data': 'Data',
      'tab.log': 'Log',
      'tab.report': 'Report',
      'report.title': 'Daily report',
      'report.hint': '2-hour buckets by day; the whole night (22:00–06:00) is one bucket',
      'report.live': 'In progress',
      'report.day': 'Day {n}',
      'report.empty': 'No stats yet — let the sim run for a while.',
      'report.emptyBucket': 'Nothing much going on',
      'log.town': 'Town log',
      'log.all': 'Whole town',
      'log.hint': 'Click a resident on the map to read their day',
      'log.empty': 'No entries yet — let the sim run for a while.',
      'log.day': 'Day {n}',
      'r.log': 'Log',
      'ev.fallbackKind': 'Interaction'
    }
  };

  var listeners = [];

  function normalize(code) {
    return DICT[code] ? code : 'en';
  }

  function readStored() {
    try { return localStorage.getItem(KEY); } catch (_) { return null; }
  }

  // 默认英文：第一次进（localStorage 里没存过）看到的就是英文。
  // 你在右上角设置里选过以后，就一直按你选的来。
  var lang = normalize(readStored() || 'en');

  // 取文案：当前语言缺失时回落中文，再缺失就返回 key（便于发现漏配）
  function t(key, vars) {
    var table = DICT[lang] || DICT.zh;
    var s = table[key];
    if (s == null) s = DICT.zh[key];
    if (s == null) return key;
    if (vars) {
      for (var k in vars) {
        if (Object.prototype.hasOwnProperty.call(vars, k)) {
          s = s.split('{' + k + '}').join(vars[k]);
        }
      }
    }
    return s;
  }

  // 二选一：en 为空/未传时回落中文
  function L(zh, en) {
    return lang === 'en' ? ((en == null || en === '') ? zh : en) : zh;
  }

  function getLang() { return lang; }

  // 静态文案：data-i18n（textContent）/ data-i18n-title（title 属性）
  function applyStatic(root) {
    var scope = root || document;
    var nodes = scope.querySelectorAll('[data-i18n]');
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].textContent = t(nodes[i].getAttribute('data-i18n'));
    }
    var titled = scope.querySelectorAll('[data-i18n-title]');
    for (var j = 0; j < titled.length; j++) {
      titled[j].title = t(titled[j].getAttribute('data-i18n-title'));
    }
    document.documentElement.lang = lang === 'en' ? 'en' : 'zh-CN';
    document.documentElement.setAttribute('data-lang', lang);
    document.title = t('app.title');
  }

  function setLang(code) {
    var next = normalize(code);
    lang = next;
    try { localStorage.setItem(KEY, lang); } catch (_) { /* 隐私模式下忽略 */ }
    applyStatic();
    for (var i = 0; i < listeners.length; i++) {
      try { listeners[i](lang); } catch (e) { /* 单个监听器出错不影响其它 */ }
    }
  }

  function onLang(fn) {
    if (typeof fn === 'function') listeners.push(fn);
  }

  global.i18n = {
    t: t, L: L, getLang: getLang, setLang: setLang,
    applyStatic: applyStatic, onLang: onLang, LANGS: LANGS
  };

  // 页面一加载就套用已保存的语言（含 <title> 与 <html lang>）
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { applyStatic(); });
  } else {
    applyStatic();
  }
})(window);
