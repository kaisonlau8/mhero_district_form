/* feedback.js — Ant Design 交互反馈（Button loading / Spin 由页面标记，message 轻提示）。
 * 无第三方动效库。页面约定：
 * - <button data-loading-text="正在保存"> 所在表单提交后进入 loading，并弹出 message.loading
 * - <a data-nav data-loading-text="正在打开账号设置"> 同页跳转
 * - <a data-nav="blank" data-success-text="已在新标签页打开「…」" data-error-text="…"> 新标签
 * - 点击被禁用控件的包裹元素 [data-blocked-text] 时弹出错误提示
 * - #flash-ok / #flash-err 的文案在加载后用 message 再提示一次
 * 暴露 window.MheroFeedback.toast(type, text) ，type = success | error | info | warning | loading
 */
(function () {
  var ICONS = { success: "check-circle", error: "close-circle", warning: "exclamation-circle", info: "info-circle" };

  function loadingSvg() {
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "ai anticon-spin");
    svg.setAttribute("viewBox", "0 0 1024 1024");
    svg.setAttribute("aria-hidden", "true");
    var use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#i-loading");
    svg.appendChild(use);
    return svg;
  }

  function root() {
    var el = document.querySelector(".ant-message");
    if (!el) {
      el = document.createElement("div");
      el.className = "ant-message";
      el.setAttribute("aria-live", "polite");
      document.body.appendChild(el);
    }
    return el;
  }

  function toast(type, text) {
    text = (text || "").trim();
    if (!text) return null;
    var notice = document.createElement("div");
    notice.className = "ant-message-notice";
    var content = document.createElement("div");
    content.className = "ant-message-notice-content";
    var row = document.createElement("div");
    row.className = "ant-message-custom-content ant-message-" + type;
    if (type === "loading") {
      var icon = document.createElement("span");
      icon.className = "ant-message-icon";
      icon.appendChild(loadingSvg());
      row.appendChild(icon);
    } else if (ICONS[type]) {
      var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("class", "ai");
      svg.setAttribute("aria-hidden", "true");
      var use = document.createElementNS("http://www.w3.org/2000/svg", "use");
      use.setAttribute("href", "#i-" + ICONS[type]);
      svg.appendChild(use);
      row.appendChild(svg);
    }
    var label = document.createElement("span");
    label.className = "ant-message-text";
    label.textContent = text;
    row.appendChild(label);
    content.appendChild(row);
    notice.appendChild(content);
    root().appendChild(notice);
    function close() {
      if (!notice.isConnected || notice.classList.contains("ant-message-notice-leave")) return;
      notice.classList.add("ant-message-notice-leave");
      setTimeout(function () { notice.remove(); }, 200);
    }
    if (type !== "loading") setTimeout(close, type === "error" ? 4000 : 3000);
    return { close: close };
  }

  function setLoading(el) {
    if (!el || el.getAttribute("data-loading") === "1") return;
    el.setAttribute("data-loading", "1");
    el.setAttribute("aria-busy", "true");
    el.classList.add("ant-btn-loading");
    Array.prototype.forEach.call(el.children, function (ch) {
      if (ch.classList && ch.classList.contains("ai")) ch.setAttribute("data-fb-hidden", "1");
    });
    var icon = document.createElement("span");
    icon.className = "ant-btn-loading-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.appendChild(loadingSvg());
    el.insertBefore(icon, el.firstChild);
    setTimeout(function () {
      if (el.getAttribute("data-loading") !== "1") return;
      el.classList.add("is-locked");
      if (el.tagName === "BUTTON" && !el.disabled) {
        el.disabled = true;
        el.setAttribute("data-fb-dis", "1");
      }
    }, 0);
  }

  function clearLoading(el) {
    if (!el) return;
    el.classList.remove("ant-btn-loading", "is-locked");
    el.removeAttribute("data-loading");
    el.removeAttribute("aria-busy");
    var icon = el.querySelector(":scope > .ant-btn-loading-icon");
    if (icon) icon.remove();
    Array.prototype.forEach.call(el.querySelectorAll("[data-fb-hidden]"), function (n) {
      n.removeAttribute("data-fb-hidden");
    });
    if (el.getAttribute("data-fb-dis") === "1") {
      el.disabled = false;
      el.removeAttribute("data-fb-dis");
    }
  }

  function blocked(el) {
    return el.hasAttribute("disabled") || el.getAttribute("aria-disabled") === "true";
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form || form.nodeName !== "FORM" || form.hasAttribute("data-feedback-off")) return;
    var btn = e.submitter && e.submitter.tagName === "BUTTON" ? e.submitter : form.querySelector('button[type="submit"], button:not([type])');
    if (!btn) return;
    if (btn.getAttribute("data-loading") === "1") {
      e.preventDefault();
      return;
    }
    setLoading(btn);
    toast("loading", btn.getAttribute("data-loading-text") || "正在提交");
  }, true);

  document.addEventListener("click", function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var wrap = e.target.closest && e.target.closest("[data-blocked-text]");
    var nav = e.target.closest && e.target.closest("[data-nav]");
    if (wrap && (!nav || !wrap.contains(nav) || (nav && blocked(nav) && !nav.contains(e.target)))) {
      var ctrl = wrap.querySelector("[data-nav]");
      if (ctrl && blocked(ctrl)) {
        e.preventDefault();
        toast("error", wrap.getAttribute("data-blocked-text") || ctrl.getAttribute("data-error-text") || "暂时无法打开");
        return;
      }
    }
    if (!nav) return;
    if (nav.getAttribute("data-loading") === "1") {
      e.preventDefault();
      return;
    }
    if (blocked(nav)) {
      e.preventDefault();
      toast("error", nav.getAttribute("data-error-text") || "暂时无法打开");
      return;
    }
    setLoading(nav);
    if (nav.getAttribute("data-nav") === "blank") {
      var ok = nav.getAttribute("data-success-text");
      if (ok) toast("success", ok);
      setTimeout(function () { clearLoading(nav); }, 700);
    } else {
      var pending = nav.getAttribute("data-loading-text");
      if (pending) toast("loading", pending);
    }
  });

  var ok = document.querySelector("#flash-ok .ant-alert-content");
  var err = document.querySelector("#flash-err .ant-alert-content");
  if (ok) toast("success", ok.textContent);
  if (err) toast("error", err.textContent);

  window.MheroFeedback = { toast: toast, loading: setLoading, clear: clearLoading };
})();
