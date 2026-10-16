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
