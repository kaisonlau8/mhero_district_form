/* antd-feedback — Ant Design 交互反馈（Button loading / Spin / Upload / message）
   全站一份：登录页与 base 模板共用。不依赖构建，不改按钮文案与配色。 */
(function () {
  var ICONS = { success: "i-check-circle", error: "i-close-circle", info: "i-info-circle", warning: "i-warning" };
  var toasted = {};

  function textOf(el) {
    return (el && el.textContent || "").replace(/\s+/g, " ").trim();
  }

  function holder() {
    var el = document.querySelector(".ant-message");
    if (!el) {
      el = document.createElement("div");
      el.className = "ant-message";
      el.setAttribute("role", "status");
      el.setAttribute("aria-live", "polite");
      document.body.appendChild(el);
    }
    return el;
  }

  function message(type, text) {
    if (!text) return;
    var kind = ICONS[type] ? type : "info";
    var notice = document.createElement("div");
    notice.className = "ant-message-notice ant-message-" + kind;
    notice.innerHTML = '<svg class="ai" aria-hidden="true"><use href="#' + ICONS[kind] + '"/></svg><span class="ant-message-notice-content"></span>';
    notice.querySelector(".ant-message-notice-content").textContent = text;
    holder().appendChild(notice);
    window.setTimeout(function () {
      if (notice.parentNode) notice.parentNode.removeChild(notice);
    }, kind === "error" ? 4500 : 3000);
  }

  function setButtonLoading(btn, on) {
    if (!btn) return;
    if (on) {
      if (btn.getAttribute("data-fb-loading") === "1") return;
      btn.setAttribute("data-fb-loading", "1");
      btn.classList.add("ant-btn-loading");
      btn.setAttribute("aria-busy", "true");
      if (btn.tagName === "BUTTON" || btn.tagName === "INPUT") {
        btn.setAttribute("data-fb-was-disabled", btn.disabled ? "1" : "0");
        btn.disabled = true;
      } else {
        btn.setAttribute("aria-disabled", "true");
      }
      var icon = document.createElement("span");
      icon.className = "ant-btn-loading-icon";
      icon.innerHTML = '<svg class="ai anticon-spin" aria-hidden="true"><use href="#i-loading"/></svg>';
      btn.insertBefore(icon, btn.firstChild);
      return;
    }
    btn.removeAttribute("data-fb-loading");
    btn.classList.remove("ant-btn-loading");
    btn.removeAttribute("aria-busy");
    btn.removeAttribute("aria-disabled");
    var mark = btn.querySelector(".ant-btn-loading-icon");
    if (mark) mark.remove();
    if (btn.tagName === "BUTTON" || btn.tagName === "INPUT") {
      btn.disabled = btn.getAttribute("data-fb-was-disabled") === "1";
      btn.removeAttribute("data-fb-was-disabled");
    }
  }

  function lockSiblings(form, btn) {
    form.querySelectorAll('button[type="submit"], button:not([type]), input[type="submit"]').forEach(function (b) {
      if (b === btn || b.getAttribute("data-fb-loading") === "1") return;
      if (!b.getAttribute("data-fb-was-disabled")) b.setAttribute("data-fb-was-disabled", b.disabled ? "1" : "0");
      b.disabled = true;
    });
  }

  function unlockForm(form) {
    if (!form) return;
    form.querySelectorAll("[data-fb-loading], [data-fb-was-disabled]").forEach(function (el) {
      if (el.getAttribute("data-fb-loading") === "1") setButtonLoading(el, false);
      else if (el.tagName === "BUTTON" || el.tagName === "INPUT") {
        el.disabled = el.getAttribute("data-fb-was-disabled") === "1";
        el.removeAttribute("data-fb-was-disabled");
      }
    });
  }

  function showPageSpin(label) {
    var el = document.getElementById("antd-page-spin");
    if (!el) {
      el = document.createElement("div");
      el.id = "antd-page-spin";
      el.className = "ant-spin-fullscreen";
      el.setAttribute("role", "status");
      el.setAttribute("aria-live", "polite");
      el.innerHTML = '<div class="ant-spin ant-spin-lg ant-spin-spinning"><span class="ant-spin-dot ant-spin-dot-spin">'
        + '<i class="ant-spin-dot-item"></i><i class="ant-spin-dot-item"></i><i class="ant-spin-dot-item"></i><i class="ant-spin-dot-item"></i>'
        + '</span><div class="ant-spin-text"></div></div>';
      document.body.appendChild(el);
    }
    el.querySelector(".ant-spin-text").textContent = label || "正在加载…";
    document.body.setAttribute("aria-busy", "true");
  }

  function hidePageSpin() {
    var el = document.getElementById("antd-page-spin");
    if (el && el.parentNode) el.parentNode.removeChild(el);
    document.body.removeAttribute("aria-busy");
  }

  function spinText(form, btn) {
    var t = (textOf(btn) || "").replace(/\s/g, "");
    var method = (form.getAttribute("method") || "get").toLowerCase();
    var action = form.getAttribute("action") || "";
    if (t.indexOf("退出") >= 0 || action.indexOf("/logout") >= 0) return "正在退出…";
    if (t.indexOf("登录") >= 0 || action.indexOf("/login") >= 0) return "正在登录…";
    if (method === "get" || t.indexOf("筛选") >= 0 || t.indexOf("搜索") >= 0 || t.indexOf("预览") >= 0) return "正在筛选…";
    return "正在提交…";
  }

  function isModified(e) {
    return e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey;
  }

  function isExportLink(a, href) {
    if (a.classList.contains("btn-export")) return true;
    return /\.xlsx(\?|#|$)/i.test(href);
  }

  function isLoginLink(a, href) {
    var t = textOf(a).replace(/\s/g, "");
    return t.indexOf("登录") >= 0 || t.indexOf("退出") >= 0 || /\/logout|\/auth\/|\/login/.test(href);
  }

  function filenameFromDisposition(cd) {
    if (!cd) return "";
    var star = /filename\*=(?:UTF-8''|utf-8'')([^;]+)/i.exec(cd);
    if (star) {
      try { return decodeURIComponent(star[1].trim().replace(/^"|"$/g, "")); } catch (e) { return star[1]; }
    }
    var plain = /filename="?([^";]+)"?/i.exec(cd);
    return plain ? plain[1] : "";
  }

  function saveBlob(blob, name) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = name || "导出.xlsx";
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
  }

  function startExport(a, href) {
    setButtonLoading(a, true);
    message("info", "正在导出…");
    var xhr = new XMLHttpRequest();
    xhr.open("GET", href);
    xhr.responseType = "blob";
    xhr.onload = function () {
      var ct = (xhr.getResponseHeader("Content-Type") || "").toLowerCase();
      var ok = xhr.status >= 200 && xhr.status < 300 && ct.indexOf("text/html") < 0 && ct.indexOf("json") < 0;
      setButtonLoading(a, false);
      var sentToLogin = (xhr.responseURL || "").indexOf("/login") >= 0;
      if (!ok || !xhr.response || !xhr.response.size) {
        message("error", xhr.status === 401 || sentToLogin ? "导出失败，请重新登录" : "导出失败，请稍后重试");
        return;
      }
      var name = filenameFromDisposition(xhr.getResponseHeader("Content-Disposition"));
      if (!name) {
        try { name = decodeURIComponent(new URL(href, window.location.href).pathname.split("/").pop() || ""); } catch (e) { name = "导出.xlsx"; }
      }
      saveBlob(xhr.response, name);
      message("success", "已开始下载");
    };
    xhr.onerror = function () {
      setButtonLoading(a, false);
      message("error", "导出失败，请检查网络后重试");
    };
    xhr.send();
  }

  function uploadList(input, filename) {
    var list = input.parentNode.querySelector(".ant-upload-list");
    if (!list) {
      list = document.createElement("div");
      list.className = "ant-upload-list";
      list.innerHTML = '<div class="ant-upload-list-item"><span class="ant-upload-list-item-name"></span>'
        + '<div class="ant-progress ant-progress-line ant-progress-status-active" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">'
        + '<div class="ant-progress-outer"><div class="ant-progress-inner"><div class="ant-progress-bg"></div></div></div>'
        + '<span class="ant-progress-text">0%</span></div></div>';
      input.insertAdjacentElement("afterend", list);
    }
    list.querySelector(".ant-upload-list-item-name").textContent = filename;
    return list;
  }

  function setUploadProgress(list, percent, label) {
    var bar = list.querySelector(".ant-progress");
    var bg = list.querySelector(".ant-progress-bg");
    var text = list.querySelector(".ant-progress-text");
    var now = Math.max(0, Math.min(100, Math.round(percent)));
    bar.setAttribute("aria-valuenow", String(now));
    bar.classList.remove("ant-progress-status-exception");
    if (now >= 100) {
      bar.classList.remove("ant-progress-status-active");
      bar.classList.add("ant-progress-status-success");
    }
    bg.style.width = now + "%";
    text.textContent = label || (now + "%");
  }

  function readError(xhr, fallback) {
    try {
      var ct = (xhr.getResponseHeader("Content-Type") || "").toLowerCase();
      if (ct.indexOf("json") >= 0 && xhr.responseText) {
        var data = JSON.parse(xhr.responseText);
        if (typeof data.detail === "string" && data.detail) return data.detail;
      }
    } catch (e) { /* 保持兜底文案 */ }
    return fallback;
  }

  function startUpload(form, btn) {
    var input = form.querySelector('input[type="file"]');
    var file = input && input.files && input.files[0];
    if (!file) {
      unlockForm(form);
      message("error", "请选择要上传的文件");
      return;
    }
    var list = uploadList(input, file.name);
    setUploadProgress(list, 0, "0%");
    var xhr = new XMLHttpRequest();
    xhr.open((form.getAttribute("method") || "POST").toUpperCase(), form.getAttribute("action") || window.location.href);
    xhr.upload.onprogress = function (ev) {
      if (!ev.lengthComputable) {
        setUploadProgress(list, 40, "正在上传…");
        return;
      }
      var pct = Math.round(ev.loaded / ev.total * 100);
      setUploadProgress(list, pct, pct >= 100 ? "正在处理…" : pct + "%");
    };
    xhr.onload = function () {
      var ct = (xhr.getResponseHeader("Content-Type") || "").toLowerCase();
      if (xhr.status >= 200 && xhr.status < 300 && ct.indexOf("text/html") >= 0) {
        setUploadProgress(list, 100, "正在打开页面…");
        showPageSpin("正在打开页面…");
        window.location.assign(xhr.responseURL || window.location.href);
        return;
      }
      var loc = xhr.getResponseHeader("Location");
      if (xhr.status >= 300 && xhr.status < 400 && loc) {
        showPageSpin("正在打开页面…");
        window.location.assign(loc);
        return;
      }
      var bar = list.querySelector(".ant-progress");
      bar.classList.add("ant-progress-status-exception");
      unlockForm(form);
      message("error", readError(xhr, "上传失败，请稍后重试"));
    };
    xhr.onerror = function () {
      var bar = list.querySelector(".ant-progress");
      if (bar) bar.classList.add("ant-progress-status-exception");
      unlockForm(form);
      message("error", "上传失败，请检查网络后重试");
    };
    xhr.send(new FormData(form));
  }

  function bootToasts() {
    var nodes = document.querySelectorAll(".ant-alert-success .ant-alert-content, .ant-alert-error .ant-alert-content, .row-msg");
    var shown = 0;
    nodes.forEach(function (el) {
      var text = textOf(el);
      if (!text || toasted[text] || shown >= 3) return;
      toasted[text] = 1;
      shown += 1;
      var type = "info";
      if (el.classList.contains("row-msg-err") || el.closest(".ant-alert-error")) type = "error";
      else if (el.classList.contains("row-msg-ok") || el.closest(".ant-alert-success")) type = "success";
      message(type, text);
    });
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form || form.tagName !== "FORM" || e.defaultPrevented) return;
    var btn = e.submitter || form.querySelector('button[type="submit"], button:not([type]), input[type="submit"]');
    if (btn) setButtonLoading(btn, true);
    lockSiblings(form, btn);
    if (form.querySelector('input[type="file"]')) {
      e.preventDefault();
      startUpload(form, btn);
      return;
    }
    showPageSpin(spinText(form, btn));
  });

  document.addEventListener("change", function (e) {
    var el = e.target;
    if (!el || !el.form) return;
    var oc = el.getAttribute("onchange") || "";
    if (!/form\.submit\s*\(/.test(oc)) return;
    showPageSpin("正在筛选…");
  });

  document.addEventListener("click", function (e) {
    if (isModified(e)) return;
    var a = e.target.closest ? e.target.closest("a[href]") : null;
    if (!a) {
      var host = e.target.closest ? e.target.closest("[onclick]") : null;
      if (host && /location\.href/.test(host.getAttribute("onclick") || "")) showPageSpin("正在加载…");
      return;
    }
    if (a.target && a.target !== "_self") return;
    var href = a.getAttribute("href") || "";
    if (!href || href.charAt(0) === "#" || href.indexOf("javascript:") === 0) return;
    if (isExportLink(a, href)) {
      e.preventDefault();
      startExport(a, href);
      return;
    }
    var url;
    try { url = new URL(href, window.location.href); } catch (err) { return; }
    if (url.origin !== window.location.origin) return;
    if (url.pathname === window.location.pathname && url.search === window.location.search && url.hash) return;
    showPageSpin(isLoginLink(a, href) ? "正在登录…" : "正在加载…");
    if (isLoginLink(a, href)) setButtonLoading(a, true);
  });

  window.addEventListener("pageshow", function () {
    hidePageSpin();
    document.querySelectorAll("[data-fb-loading='1']").forEach(function (el) { setButtonLoading(el, false); });
  });

  bootToasts();
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bootToasts);

  window.antdFeedback = { message: message, showPageSpin: showPageSpin, hidePageSpin: hidePageSpin, setButtonLoading: setButtonLoading };
})();
