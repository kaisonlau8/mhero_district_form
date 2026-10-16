/* cards.js — 表格移动端卡片化（配 antd-lite.css 9.6 节，<=768px 生效）
 * 通用机制，各站点在 base 模板尾部内联引入（{{ ds_cards|safe }} 形式）。
 * - 自动给滚动容器（.tw/.ant-table-wrap/.sheet/.store-table-wrapper/.freeze-wrap）里的表格
 *   加 .ant-table-cards，容器加 .ant-table-wrap-cards；
 * - 从 thead 第一个 tr 的 th 文本给 td 补 data-label（已有不覆盖）；
 *   th 内的 <small> 副标题会并进标签（如「首保完成率 已保/应保」）；
 * - 卡片标题：th[data-card-title] 所在列的 td 标 data-card-title；
 *   未显式指定时回退为第一个「非 #/名次/排名/编号 且非空」的列；
 * - 控件列（含 button/input/select/textarea/form 或「纯链接」的单元格）标 data-cards-full，
 *   CSS 里整行宽、触控 >=44px；
 * - colspan 行（空表）、无 thead 的表格原样跳过；table[data-cards="off"] 可退出；
 * - MutationObserver 兜底：JS 后插入的行/表头（NSS、超时机器人等）会补标。
 * 桌面端只添加无害属性，不改任何布局与行为。 */
(function () {
  'use strict';
  var WRAP = '.tw,.ant-table-wrap,.sheet,.store-table-wrapper,.freeze-wrap';
  var SKIP_TITLES = { '#': 1, '名次': 1, '排名': 1, '编号': 1, '序号': 1 };
  var sigs = new WeakMap();

  function headCells(t) {
    if (!t.tHead || !t.tHead.rows.length) return [];
    return [].slice.call(t.tHead.rows[0].cells);
  }

  function labelsOf(t) {
    return headCells(t).map(function (th) {
      return (th.innerText || th.textContent || '').replace(/\s+/g, ' ').trim();
    });
  }

  function markRows(t, force) {
    var labels = labelsOf(t);
    if (!labels.length) return;
    var ths = headCells(t), titleIdx = -1, fallback = -1;
    for (var i = 0; i < ths.length; i++) {
      if (ths[i].hasAttribute('data-card-title')) { titleIdx = i; break; }
      if (fallback < 0 && labels[i] && !SKIP_TITLES[labels[i]]) fallback = i;
    }
    if (titleIdx < 0) titleIdx = fallback;
    [].forEach.call(t.tBodies, function (tb) {
      [].forEach.call(tb.rows, function (tr) {
        [].forEach.call(tr.cells, function (td) {
          var i = td.cellIndex;
          if (td.hasAttribute('colspan')) return;
          if (i < labels.length) {
            if (labels[i]) {
              if (force || !td.hasAttribute('data-label')) td.setAttribute('data-label', labels[i]);
            } else if (force) {
              td.removeAttribute('data-label');
            }
          }
          if (i === titleIdx) td.setAttribute('data-card-title', '');
          if (!td.hasAttribute('data-cards-full')) {
            var ctl = td.querySelector('button,input:not([type="hidden"]),select,textarea,form,.ant-btn,.btn');
            if (!ctl) {
              var probe = td.cloneNode(true);
              [].forEach.call(probe.querySelectorAll('a'), function (a) { a.remove(); });
              if (!(probe.textContent || '').trim() && td.querySelector('a')) ctl = true;
            }
            if (ctl) td.setAttribute('data-cards-full', '');
          }
        });
      });
    });
  }

  function upgrade(t) {
    if (t.getAttribute('data-cards') === 'off') return;
    if (t.classList.contains('ant-table-cards')) { markRows(t, false); sigs.set(t, labelsOf(t).join('|')); return; }
    if (!t.tHead || !t.tHead.rows.length || !t.tBodies.length) return;
    var labels = labelsOf(t);
    if (!labels.length || !labels.some(Boolean)) return;
    t.classList.add('ant-table-cards');
    var w = t.closest ? t.closest(WRAP) : null;
    if (w) w.classList.add('ant-table-wrap-cards');
    markRows(t, false);
    sigs.set(t, labels.join('|'));
  }

  function scan() {
    [].forEach.call(document.querySelectorAll('table'), function (t) {
      if (t.classList.contains('ant-table-cards')) {
        var sig = labelsOf(t).join('|');
        if (sigs.get(t) !== sig) markRows(t, true);   /* 动态重建表头（如超时机器人）：重标 */
        sigs.set(t, sig);
        return;
      }
      if (t.closest && t.closest(WRAP)) upgrade(t);
    });
  }

  var pending = null;
  function schedule() {
    if (pending) return;
    pending = setTimeout(function () { pending = null; scan(); }, 50);
  }

  scan();
  if (document.body) {
    new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
  } else {
    document.addEventListener('DOMContentLoaded', scan);
  }
})();

/* 移动端导航（<=860px，与 antd-lite.css 4.5–4.7 同一断点）。
 * 统一抽屉契约（配套 antd-lite.css 4.6）：
 * - 断点唯一：max-width 860px；桌面端不改顶栏。
 * - 自动汉堡：仅当顶栏（.topbar/.ant-header）里没有 .burger、没有 [data-burger]、
 *   没有任何 #burger / [id$="-burger"] / [class$="-burger"]（页面自带汉堡的常见命名），
 *   且顶栏及其祖先都没有 data-no-auto-burger 时，才插入一只按钮（data-mhero-burger
 *   标记）并把顶栏里的 .menu/.ant-menu/裸 nav 收成右侧抽屉。
 * - 页面自带按钮（class="burger" 或 data-burger 属性）：不插第二只、不接管开合
 *   （开合仍由页面 JS 切 .topbar.open / .ant-header.open / body.nav-open 驱动），
 *   这里只补四种关闭行为：遮罩点击、Escape、点击菜单链接、断点切回桌面。
 * - 幂等：脚本被引入两次时 window.__mheroMobileNav 保证只初始化一次
 *   （按钮只插一只、document 级事件只绑一次）。
 * - 黄页 .hub-user 仍用页面自己的点击切换，这里补姓名行、aria 与遮罩关闭。 */
(function () {
  'use strict';
  if (window.__mheroMobileNav) return;
  window.__mheroMobileNav = true;

  var mq = window.matchMedia('(max-width: 860px)');
  var OWN = '.burger, [data-burger]';

  function setBurger(btn, open) {
    if (!btn) return;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    var label = btn.getAttribute('aria-label') || '';
    if (btn.getAttribute('data-mhero-burger') === '1' || label === '打开菜单' || label === '关闭菜单') {
      btn.setAttribute('aria-label', open ? '关闭菜单' : '打开菜单');
    }
  }

  function closeBar(bar) {
    bar.classList.remove('open');
    setBurger(bar.querySelector(OWN), false);
  }

  function navCandidate(header) {
    var box = header.querySelector(':scope > .in') || header;
    return box.querySelector(':scope > .menu, :scope > .ant-menu') || box.querySelector(':scope > nav');
  }

  function ensureAutoBurger(header) {
    if (header.querySelector(OWN)) return;                          /* 已有 .burger / [data-burger] */
    if (header.querySelector('#burger, [id$="-burger"], [class$="-burger"]')) return; /* 自带命名的汉堡 */
    if (header.closest('[data-no-auto-burger]')) return;            /* 顶栏或祖先显式退出 */
    if (header.querySelector('.burger[data-mhero-burger]')) return; /* 幂等：这只顶栏已插过 */
    var nav = navCandidate(header);
    if (!nav) return;
    if (!nav.id) nav.id = 'mainmenu';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'burger';
    btn.setAttribute('data-mhero-burger', '1');
    btn.setAttribute('aria-controls', nav.id);
    btn.setAttribute('aria-expanded', 'false');
    btn.setAttribute('aria-label', '打开菜单');
    nav.parentNode.insertBefore(btn, nav);
    if (!nav.classList.contains('menu') && !nav.classList.contains('ant-menu')) {
      /* 无 ds 菜单 class 的裸 nav：加 .has-burger 才有抽屉样式；
         .nav-ready 两帧后再上，首次加载才不会从水平菜单位置滑出 */
      header.classList.add('has-burger');
      requestAnimationFrame(function () {
        requestAnimationFrame(function () { header.classList.add('nav-ready'); });
      });
    }
  }

  function ensureHubWho(u) {
    var dd = u.querySelector('.dd');
    if (!dd || dd.querySelector('.hub-drawer-who')) return;
    var who = document.createElement('div');
    who.className = 'hub-drawer-who';
    var name = u.querySelector('.n');
    var tag = u.querySelector('.ant-tag');
    who.textContent = name ? name.textContent.trim() : '';
    if (tag && tag.textContent.trim()) {
      who.appendChild(document.createTextNode(' '));
      var pill = document.createElement('span');
      pill.className = tag.className;
      pill.textContent = tag.textContent.trim();
      who.appendChild(pill);
    }
    dd.insertBefore(who, dd.firstChild);
  }

  function syncHubLabel() {
    var u = document.querySelector('.hub-user');
    if (!u) return;
    var btn = u.querySelector(':scope > button');
    if (!btn) return;
    if (!mq.matches) {
      if (btn.hasAttribute('data-hub-aria')) {
        btn.removeAttribute('aria-label');
        btn.removeAttribute('data-hub-aria');
      }
      return;
    }
    ensureHubWho(u);
    if (!u.classList.contains('open')) btn.setAttribute('aria-label', '打开菜单');
    btn.setAttribute('data-hub-aria', '1');
  }

  function boot() {
    [].forEach.call(document.querySelectorAll('.topbar, .ant-header'), ensureAutoBurger);
    syncHubLabel();
  }

  document.addEventListener('click', function (e) {
    var t = e.target;
    if (!t || !t.closest) return;

    /* 黄页 .hub-user 打开时点自身（遮罩区域）关闭 */
    if (t.classList && t.classList.contains('hub-user') && t.classList.contains('open')) {
      t.classList.remove('open');
      var hb = t.querySelector(':scope > button');
      if (hb) {
        hb.setAttribute('aria-expanded', 'false');
        hb.setAttribute('aria-label', '打开菜单');
      }
      return;
    }

    /* 本脚本插入的汉堡：开合在这里驱动（页面自带按钮的开合由页面自己驱动） */
    var own = t.closest('.burger[data-mhero-burger], [data-burger][data-mhero-burger]');
    if (own) {
      e.stopPropagation();
      var hdr = own.closest('.topbar, .ant-header');
      if (hdr) {
        var open = !hdr.classList.contains('open');
        hdr.classList.toggle('open', open);
        setBurger(own, open);
      }
      return;
    }

    /* 无页面脚本的顶栏：抽屉里的下拉分组按钮也能展开/收起
       （reportdb 这类自带处理的页面在自己的 handler 里 stopPropagation，不会走到这） */
    var tog = t.closest('.mi > button, .ant-menu-item > button');
    if (tog) {
      var mi = tog.closest('.mi, .ant-menu-item');
      if (mi) {
        var was = mi.classList.contains('open');
        [].forEach.call(mi.parentNode ? mi.parentNode.children : [], function (sib) {
          if (sib !== mi) sib.classList.remove('open');
        });
        mi.classList.toggle('open', !was);
        tog.setAttribute('aria-expanded', !was ? 'true' : 'false');
      }
      return;
    }

    if (!mq.matches) return;
    var bar = t.closest('.topbar.open, .ant-header.open');
    if (!bar) return;
    /* 点抽屉里的链接（含下拉项 / 表单提交按钮）即导航：收起抽屉 */
    if (t.closest('a, button.lnk')) { closeBar(bar); return; }
    /* 点到遮罩（伪元素遮罩的点击落在顶栏本体）或顶栏空白处：关闭 */
    if (t.closest('.menu, .ant-menu, nav, .burger, [data-burger], button')) return;
    closeBar(bar);
  });

  /* 黄页按钮自己的脚本会 stopPropagation，捕获阶段才能在切换之后读到 .open */
  document.addEventListener('click', function (e) {
    var btn = e.target && e.target.closest && e.target.closest('.hub-user > button');
    if (!btn || !mq.matches) return;
    var u = btn.parentNode;
    setTimeout(function () {
      var open = u.classList.contains('open');
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      btn.setAttribute('aria-label', open ? '关闭菜单' : '打开菜单');
    }, 0);
  }, true);

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    [].forEach.call(document.querySelectorAll('.topbar.open, .ant-header.open'), closeBar);
    var u = document.querySelector('.hub-user.open');
    if (!u) return;
    u.classList.remove('open');
    var hb = u.querySelector(':scope > button');
    if (hb) {
      hb.setAttribute('aria-expanded', 'false');
      hb.setAttribute('aria-label', '打开菜单');
    }
  });

  if (mq.addEventListener) {
    mq.addEventListener('change', function () {
      if (mq.matches) return;
      [].forEach.call(document.querySelectorAll('.topbar.open, .ant-header.open'), closeBar);
      var u = document.querySelector('.hub-user.open');
      if (u) u.classList.remove('open');
      syncHubLabel();
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
