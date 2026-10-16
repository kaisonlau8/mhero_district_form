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

/* 移动端导航（<=860px，与 antd-lite.css 4.6 / 4.7 同一断点）。
 * 桌面端不改顶栏。已有 #burger 的页面（reportdb）只补遮罩点击与 Esc；
 * 只有水平 nav、没有汉堡的顶栏（VIP）在窄屏插入按钮并加上 .has-burger；
 * 黄页 .hub-user 仍用页面自己的点击切换，这里补姓名行、aria 与遮罩关闭。 */
(function () {
  'use strict';
  var mq = window.matchMedia('(max-width: 860px)');

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
    setBurger(bar.querySelector('.burger'), false);
  }

  function ensurePlainNav(header) {
    if (header.querySelector('.burger')) return;
    var nav = header.querySelector(':scope > .in > nav');
    if (!nav || nav.classList.contains('menu') || nav.classList.contains('ant-menu')) return;
    if (!nav.id) nav.id = 'mainmenu';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'burger';
    btn.setAttribute('data-mhero-burger', '1');
    btn.setAttribute('aria-controls', nav.id);
    setBurger(btn, false);
    nav.parentNode.insertBefore(btn, nav);
    header.classList.add('has-burger');
    requestAnimationFrame(function () {
      requestAnimationFrame(function () { header.classList.add('nav-ready'); });
    });
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = !header.classList.contains('open');
      header.classList.toggle('open', open);
      setBurger(btn, open);
    });
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
    [].forEach.call(document.querySelectorAll('header.topbar, header.ant-header'), ensurePlainNav);
    syncHubLabel();
  }

  document.addEventListener('click', function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    if (t.classList && t.classList.contains('hub-user') && t.classList.contains('open')) {
      t.classList.remove('open');
      var hb = t.querySelector(':scope > button');
      if (hb) {
        hb.setAttribute('aria-expanded', 'false');
        hb.setAttribute('aria-label', '打开菜单');
      }
      return;
    }
    var bar = t.closest('.topbar.open, .ant-header.open');
    if (!bar || !mq.matches) return;
    if (t.closest('.menu, .ant-menu, .burger, nav, a, button')) return;
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
