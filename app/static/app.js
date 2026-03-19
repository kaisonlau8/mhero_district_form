const REQUIRED_TYPES = [
  { key: "stock", prefix: "门店备件库存导出", label: "门店备件库存导出", target: "备件库存明细" },
  { key: "task", prefix: "保养提醒任务列表", label: "保养提醒任务列表", target: "招揽实施率" },
  { key: "first_service", prefix: "首保实施率", label: "首保实施率", target: "首保" },
  { key: "second_service", prefix: "二保实施率", label: "二保实施率", target: "二保" },
  { key: "new_policy", prefix: "新保投保率", label: "新保投保率", target: "新保" },
  { key: "renewal", prefix: "续保投保率", label: "续保投保率", target: "续保" },
  { key: "renewal_backlog", prefix: "去年同期交付未新保车辆", label: "去年同期交付未新保车辆", target: "续保" },
];

const TEMPLATE_PREFIX = "区域各指标情况一览";

const state = {
  files: new Map(),
  template: null,
  unknownFiles: [],
  generating: false,
};

const sourceDrop = document.querySelector("#sourceDrop");
const sourceInput = document.querySelector("#sourceInput");
const templateInput = document.querySelector("#templateInput");
const pickSourceButton = document.querySelector("#pickSourceButton");
const pickTemplateButton = document.querySelector("#pickTemplateButton");
const statusGrid = document.querySelector("#statusGrid");
const messageBox = document.querySelector("#messageBox");
const templateHint = document.querySelector("#templateHint");
const generateButton = document.querySelector("#generateButton");
const query = new URLSearchParams(window.location.search);

function isDesktopApp() {
  return query.get("desktop") === "1";
}

async function waitForDesktopApi() {
  const timeoutAt = Date.now() + 5000;
  while (Date.now() < timeoutAt) {
    if (window.pywebview?.api?.save_report) {
      return window.pywebview.api;
    }
    await new Promise((resolve) => window.setTimeout(resolve, 100));
  }
  throw new Error("桌面保存能力初始化超时，请重新打开应用后再试。");
}

function normalizeName(filename) {
  return filename.replace(/\.[^.]+$/, "").replace(/\s+/g, "");
}

function detectRole(filename) {
  const normalized = normalizeName(filename);

  if (normalized.includes(TEMPLATE_PREFIX)) {
    return "template";
  }

  const matched = REQUIRED_TYPES.find((item) => normalized.startsWith(item.prefix) || normalized.includes(item.prefix));
  return matched ? matched.key : null;
}

function setMessage(text, type = "neutral") {
  messageBox.textContent = text;
  messageBox.dataset.type = type;
}

function allRequiredReady() {
  return REQUIRED_TYPES.every((item) => state.files.has(item.key));
}

function renderStatus(options = {}) {
  const { preserveMessage = false } = options;
  statusGrid.innerHTML = "";

  REQUIRED_TYPES.forEach((item) => {
    const card = document.createElement("div");
    card.className = "status-card";

    const title = document.createElement("p");
    title.className = "status-title";
    title.textContent = item.label;

    const target = document.createElement("p");
    target.className = "status-target";
    target.textContent = `写入 ${item.target}`;

    const detail = document.createElement("p");
    detail.className = "status-detail";

    const file = state.files.get(item.key);
    if (file) {
      card.dataset.ready = "true";
      detail.textContent = file.name;
    } else {
      card.dataset.ready = "false";
      detail.textContent = "未上传";
    }

    card.append(title, target, detail);
    statusGrid.append(card);
  });

  if (state.template) {
    templateHint.textContent = `当前模板：${state.template.name}`;
  } else {
    templateHint.textContent = "默认使用内置模板，也可以额外上传新的“区域各指标情况一览xxxx”。";
  }

  generateButton.disabled = !allRequiredReady() || state.generating;

  if (!state.generating && !preserveMessage) {
    if (state.unknownFiles.length > 0) {
      setMessage(`有 ${state.unknownFiles.length} 个文件未识别，已忽略。`, "warn");
    } else if (allRequiredReady()) {
      setMessage("文件已齐，可以直接生成并下载。", "success");
    } else {
      const missing = REQUIRED_TYPES.filter((item) => !state.files.has(item.key)).map((item) => item.label);
      setMessage(`还缺 ${missing.length} 份：${missing.join("、")}`, "neutral");
    }
  }
}

function absorbFiles(fileList, mode = "source") {
  const files = Array.from(fileList || []);
  if (files.length === 0) {
    return;
  }

  if (mode === "template") {
    state.template = files[0];
    renderStatus();
    return;
  }

  state.unknownFiles = [];
  files.forEach((file) => {
    const role = detectRole(file.name);
    if (role === "template") {
      state.template = file;
      return;
    }
    if (!role) {
      state.unknownFiles.push(file.name);
      return;
    }
    state.files.set(role, file);
  });

  renderStatus();
}

async function generateReport() {
  if (!allRequiredReady() || state.generating) {
    return;
  }

  const formData = new FormData();
  REQUIRED_TYPES.forEach((item) => {
    const file = state.files.get(item.key);
    formData.append("files", file, file.name);
  });

  if (state.template) {
    formData.append("template", state.template, state.template.name);
  }

  state.generating = true;
  generateButton.disabled = true;
  setMessage("正在处理 Excel 并生成下载文件，请稍等……", "neutral");

  try {
    const response = await fetch("/api/generate", {
      method: "POST",
      body: formData,
    });

    if (!response.ok) {
      const payload = await response.json().catch(() => ({ detail: "生成失败，请稍后重试。" }));
      throw new Error(payload.detail || "生成失败，请稍后重试。");
    }

    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
    const filename = match ? decodeURIComponent(match[1]) : "区域各指标情况一览_已更新.xlsx";

    if (isDesktopApp()) {
      const api = await waitForDesktopApi();
      const arrayBuffer = await blob.arrayBuffer();
      const bytes = new Uint8Array(arrayBuffer);
      let binary = "";
      const chunkSize = 0x8000;
      for (let offset = 0; offset < bytes.length; offset += chunkSize) {
        binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
      }
      const base64Content = btoa(binary);
      const result = await api.save_report(filename, base64Content);
      if (!result?.ok) {
        throw new Error(result?.message || "保存文件时失败。");
      }
      setMessage("Excel 已生成并保存。打开后，“总表”会按模板公式自动重算。", "success");
    } else {
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.append(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setMessage("Excel 已生成并开始下载。打开后，“总表”会按模板公式自动重算。", "success");
    }
  } catch (error) {
    setMessage(error.message || "生成失败，请检查文件是否完整。", "error");
  } finally {
    state.generating = false;
    renderStatus({ preserveMessage: true });
  }
}

function wireDropArea() {
  ["dragenter", "dragover"].forEach((eventName) => {
    sourceDrop.addEventListener(eventName, (event) => {
      event.preventDefault();
      sourceDrop.dataset.dragging = "true";
    });
  });

  ["dragleave", "drop"].forEach((eventName) => {
    sourceDrop.addEventListener(eventName, (event) => {
      event.preventDefault();
      sourceDrop.dataset.dragging = "false";
    });
  });

  sourceDrop.addEventListener("drop", (event) => {
    absorbFiles(event.dataTransfer.files, "source");
  });

  sourceDrop.addEventListener("click", () => sourceInput.click());
}

pickSourceButton.addEventListener("click", () => sourceInput.click());
pickTemplateButton.addEventListener("click", () => templateInput.click());
sourceInput.addEventListener("change", (event) => absorbFiles(event.target.files, "source"));
templateInput.addEventListener("change", (event) => absorbFiles(event.target.files, "template"));
generateButton.addEventListener("click", generateReport);

wireDropArea();
renderStatus();
