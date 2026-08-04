"use strict";

const elements = {
  connection: document.querySelector("#connectionBadge"),
  statusOutput: document.querySelector("#statusOutput"),
  operationOutput: document.querySelector("#operationOutput"),
  progress: document.querySelector("#progressBar"),
  refresh: document.querySelector("#refreshButton"),
  clearLog: document.querySelector("#clearLogButton"),
  defaults: document.querySelector("#defaultsButton"),
  form: document.querySelector("#configurationForm"),
  firstSummary: document.querySelector("#firstSummary"),
  secondSummary: document.querySelector("#secondSummary"),
  firstTime: document.querySelector("#firstExecutionTime"),
  firstRatio: document.querySelector("#firstCashRatio"),
  secondTime: document.querySelector("#secondExecutionTime"),
  secondRatio: document.querySelector("#secondCashRatio"),
  dialog: document.querySelector("#confirmDialog"),
  dialogTitle: document.querySelector("#confirmTitle"),
  dialogText: document.querySelector("#confirmText"),
  dialogAccept: document.querySelector("#confirmAccept"),
};

let token = "";
let defaults = null;
let busy = false;

function loadToken() {
  const hash = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const incoming = hash.get("token");
  if (incoming) {
    sessionStorage.setItem("rrLocalToken", incoming);
    history.replaceState(null, "", "/");
  }
  token = sessionStorage.getItem("rrLocalToken") || "";
  return token;
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-RR-Token", token);
  if (options.body) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function setBusy(value, message = "正在执行，请稍候…") {
  busy = value;
  document.querySelectorAll("button").forEach((button) => { button.disabled = value; });
  elements.progress.classList.toggle("hidden", !value);
  if (value) elements.operationOutput.textContent = message;
}

function setConnection(kind, text) {
  elements.connection.className = `connection ${kind}`;
  elements.connection.textContent = text;
}

function fillConfiguration(model) {
  const current = model.current;
  defaults = model.defaults;
  elements.firstTime.value = current.first_execution_time;
  elements.firstRatio.value = current.first_cash_usage_ratio;
  elements.secondTime.value = current.second_execution_time;
  elements.secondRatio.value = current.second_cash_usage_ratio;
  elements.firstSummary.textContent = `${current.first_execution_time} · ${formatRatio(current.first_cash_usage_ratio)}`;
  elements.secondSummary.textContent = `${current.second_execution_time} · ${formatRatio(current.second_cash_usage_ratio)}`;
}

function formatRatio(value) {
  return `${Math.round(Number(value) * 100)}%`;
}

async function refresh() {
  if (!token) {
    setConnection("error", "会话令牌缺失");
    elements.statusOutput.textContent = "请关闭页面并重新执行 .\\rr ui。";
    return;
  }
  try {
    const payload = await api("/api/bootstrap");
    fillConfiguration(payload.configuration);
    elements.statusOutput.textContent = payload.status.output || "状态命令没有输出。";
    setConnection(payload.status.ok ? "online" : "error", payload.status.ok ? "本机服务已连接" : "状态读取失败");
  } catch (error) {
    setConnection("error", "连接失败");
    elements.statusOutput.textContent = String(error.message || error);
  }
}

function confirmAction(title, text) {
  elements.dialogTitle.textContent = title;
  elements.dialogText.textContent = text;
  elements.dialog.showModal();
  return new Promise((resolve) => {
    elements.dialog.addEventListener("close", () => resolve(elements.dialog.returnValue === "confirm"), { once: true });
  });
}

const actionDetails = {
  verify: { title: "运行本地验证", text: "将运行完整 verify。它不连接miniQMT，也不会下单。", confirmation: null },
  off: { title: "关闭实盘任务", text: "两个实盘任务将被禁用，启用快照会撤销。之后不会自动恢复。", confirmation: "DISABLE LIVE" },
  on: { title: "启用实盘任务", text: "系统将重新核验证书、账户绑定、执行源码和四项参数；通过后，资金比例大于0的任务将进入Ready。", confirmation: "ENABLE LIVE" },
  cert_status: { title: "读取认证状态", text: "只读查看模拟认证任务，不创建或删除任务。", confirmation: null },
  stress_status: { title: "读取压力状态", text: "只读查看压力测试任务，不创建或删除任务。", confirmation: null },
  mail_test: { title: "发送测试邮件", text: "将使用本机已保存的加密SMTP配置发送一封测试邮件。", confirmation: null },
};

async function runAction(action) {
  if (busy) return;
  const detail = actionDetails[action];
  if (!detail) return;
  if (!(await confirmAction(detail.title, detail.text))) return;
  setBusy(true, `${detail.title}…`);
  try {
    const payload = await api("/api/action", {
      method: "POST",
      body: JSON.stringify({ action, confirmation: detail.confirmation }),
    });
    elements.operationOutput.textContent = payload.output || "操作完成，没有额外输出。";
    await refresh();
  } catch (error) {
    elements.operationOutput.textContent = `操作失败\n${String(error.message || error)}`;
  } finally {
    setBusy(false);
  }
}

async function saveConfiguration(event) {
  event.preventDefault();
  if (busy || !elements.form.reportValidity()) return;
  const values = {
    first_execution_time: elements.firstTime.value.trim(),
    first_cash_usage_ratio: elements.firstRatio.value.trim(),
    second_execution_time: elements.secondTime.value.trim(),
    second_cash_usage_ratio: elements.secondRatio.value.trim(),
  };
  const summary = `第一次：${values.first_execution_time} / ${formatRatio(values.first_cash_usage_ratio)}；第二次：${values.second_execution_time} / ${formatRatio(values.second_cash_usage_ratio)}。必须已经关闭实盘。`;
  if (!(await confirmAction("验证并保存参数", summary))) return;
  setBusy(true, "正在校验参数并运行完整 verify；请不要关闭窗口…");
  try {
    const payload = await api("/api/configuration", {
      method: "POST",
      body: JSON.stringify({ values, confirmation: "SAVE PARAMETERS" }),
    });
    elements.operationOutput.textContent = payload.output || "参数已验证并保存。";
    fillConfiguration(payload.configuration);
    await refresh();
  } catch (error) {
    elements.operationOutput.textContent = `参数未保存\n${String(error.message || error)}`;
  } finally {
    setBusy(false);
  }
}

elements.refresh.addEventListener("click", refresh);
elements.clearLog.addEventListener("click", () => { elements.operationOutput.textContent = "显示已清空。"; });
elements.defaults.addEventListener("click", () => {
  if (!defaults || busy) return;
  elements.firstTime.value = defaults.first_execution_time;
  elements.firstRatio.value = defaults.first_cash_usage_ratio;
  elements.secondTime.value = defaults.second_execution_time;
  elements.secondRatio.value = defaults.second_cash_usage_ratio;
});
elements.form.addEventListener("submit", saveConfiguration);
document.querySelectorAll("[data-action]").forEach((button) => {
  button.addEventListener("click", () => runAction(button.dataset.action));
});

loadToken();
refresh();
