"use strict";

const elements = {
  connection: document.querySelector("#connectionBadge"),
  taskStatusCards: document.querySelector("#taskStatusCards"),
  certificateStatus: document.querySelector("#certificateStatus"),
  operationOutput: document.querySelector("#operationOutput"),
  progress: document.querySelector("#progressBar"),
  refresh: document.querySelector("#refreshButton"),
  clearLog: document.querySelector("#clearLogButton"),
  defaults: document.querySelector("#defaultsButton"),
  form: document.querySelector("#configurationForm"),
  closeUi: document.querySelector("#closeUiButton"),
  firstTime: document.querySelector("#firstExecutionTime"),
  firstRatio: document.querySelector("#firstCashRatio"),
  secondTime: document.querySelector("#secondExecutionTime"),
  secondRatio: document.querySelector("#secondCashRatio"),
  dialog: document.querySelector("#confirmDialog"),
  dialogTitle: document.querySelector("#confirmTitle"),
  dialogText: document.querySelector("#confirmText"),
  dialogAccept: document.querySelector("#confirmAccept"),
  dialogPhraseGroup: document.querySelector("#confirmPhraseGroup"),
  dialogPhrase: document.querySelector("#confirmPhrase"),
  dialogPhraseHint: document.querySelector("#confirmPhraseHint"),
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
}

function formatRatio(value) {
  return `${Math.round(Number(value) * 100)}%`;
}

function appendStatusDetail(container, label, value) {
  const row = document.createElement("div");
  row.className = "status-detail";
  const term = document.createElement("span");
  term.textContent = label;
  const description = document.createElement("strong");
  description.textContent = value || "—";
  row.append(term, description);
  container.append(row);
}

function friendlyState(value) {
  const states = {
    ready: "已启用",
    disabled: "已禁用",
    running: "运行中",
    queued: "等待中",
  };
  return states[String(value || "").toLowerCase()] || value || "未知";
}

function friendlyBoolean(value, whenTrue, whenFalse) {
  if (String(value).toLowerCase() === "true") return whenTrue;
  if (String(value).toLowerCase() === "false") return whenFalse;
  return value || "—";
}

function friendlyStrategyParameters(value) {
  const fields = Object.fromEntries(
    String(value || "")
      .split(";")
      .map((part) => part.trim().split("=", 2))
      .filter((pair) => pair.length === 2),
  );
  const time = fields.first_order || fields.second_start;
  const ratio = fields.cash_usage;
  if (time && ratio) return `${time} / 使用资金 ${ratio}`;
  return value || "—";
}

function renderTaskStatus(tasks, statusOk) {
  elements.taskStatusCards.replaceChildren();
  if (!statusOk || !Array.isArray(tasks) || tasks.length === 0) {
    const message = document.createElement("div");
    message.className = "status-placeholder error-text";
    message.textContent = "未能提取实盘任务状态，请查看下方操作结果或重新刷新。";
    elements.taskStatusCards.append(message);
    return;
  }
  tasks.forEach((task, index) => {
    const card = document.createElement("article");
    card.className = "task-card";
    const heading = document.createElement("div");
    heading.className = "task-card-heading";
    const titleGroup = document.createElement("div");
    const stage = document.createElement("span");
    stage.className = "task-stage";
    stage.textContent = index === 0 ? "FIRST STAGE" : "SECOND STAGE";
    const title = document.createElement("h3");
    title.textContent = index === 0 ? "第一次实盘任务" : "第二次实盘任务";
    titleGroup.append(stage, title);
    const badge = document.createElement("span");
    const normalizedState = String(task.state || "Unknown").toLowerCase();
    badge.className = `state-badge ${normalizedState === "ready" ? "ready" : normalizedState === "disabled" ? "disabled" : "warning"}`;
    badge.textContent = friendlyState(task.state);
    heading.append(titleGroup, badge);
    card.append(heading);
    const details = document.createElement("div");
    details.className = "status-details";
    appendStatusDetail(details, "执行参数", friendlyStrategyParameters(task.strategy_parameters));
    appendStatusDetail(details, "计划时间", task.schedule);
    appendStatusDetail(details, "下次运行", task.next_run_time);
    appendStatusDetail(details, "任务调度", friendlyBoolean(task.schedule_matches_config, "与配置一致", "与配置不一致"));
    appendStatusDetail(details, "启用快照", task.live_enable_snapshot);
    appendStatusDetail(details, "上次运行", task.last_run_time);
    appendStatusDetail(details, "上次结果", task.last_result);
    card.append(details);
    elements.taskStatusCards.append(card);
  });
}

function renderCertification(status) {
  const valid = String(status?.valid || "false") === "true";
  elements.certificateStatus.className = `certificate-status ${valid ? "valid" : "invalid"}`;
  elements.certificateStatus.replaceChildren();
  const title = document.createElement("strong");
  title.textContent = status?.summary || "认证依据未知";
  const scope = document.createElement("span");
  scope.textContent = status?.scope || "请运行认证状态检查。";
  elements.certificateStatus.append(title, scope);
}

async function refresh() {
  if (!token) {
    setConnection("error", "会话令牌缺失");
    renderTaskStatus([], false);
    return;
  }
  try {
    const payload = await api("/api/bootstrap");
    fillConfiguration(payload.configuration);
    renderTaskStatus(payload.status.tasks, payload.status.ok);
    renderCertification(payload.status.certification);
    setConnection(payload.status.ok ? "online" : "error", payload.status.ok ? "本机服务已连接" : "状态读取失败");
  } catch (error) {
    setConnection("error", "连接失败");
    renderTaskStatus([], false);
    elements.operationOutput.textContent = `状态读取失败\n${String(error.message || error)}`;
  }
}

function confirmAction(title, text, requiredPhrase = null) {
  elements.dialogTitle.textContent = title;
  elements.dialogText.textContent = text;
  elements.dialogPhrase.value = "";
  elements.dialogPhrase.placeholder = "";
  elements.dialogPhraseHint.textContent = "";
  const needsTypedPhrase = Boolean(requiredPhrase);
  elements.dialogPhraseGroup.classList.toggle("hidden", !needsTypedPhrase);
  if (needsTypedPhrase) {
    elements.dialogPhrase.placeholder = requiredPhrase;
    elements.dialogPhraseHint.textContent =
      `请输入：${requiredPhrase}（区分大小写）`;
    elements.dialogPhrase.focus();
  }
  elements.dialog.showModal();
  return new Promise((resolve) => {
    elements.dialog.addEventListener("close", () => {
      if (elements.dialog.returnValue !== "confirm") {
        resolve(false);
        return;
      }
      resolve(requiredPhrase ? elements.dialogPhrase.value : true);
    }, { once: true });
  });
}

const actionDetails = {
  on: { title: "启用实盘任务", text: "系统将重新核验证书、账户绑定、执行源码和四项参数；通过后，资金比例大于0的任务将进入Ready。", confirmation: "ENABLE LIVE" },
  off: { title: "关闭实盘任务", text: "两个实盘任务将被禁用，启用快照会撤销。之后不会自动恢复。", confirmation: "DISABLE LIVE" },
  live_cert: { title: "快速实盘认证（固定1000元）", text: "这会提交真实GC001逆回购，累计成交本金硬上限1000元。请先确认实盘任务已关闭；成功后仍不会自动启用。", confirmation: "LIVE 1000", typed: true },
  live_cert_status: { title: "读取快速认证状态", text: "只读核验证书、journal与当前环境，不连接miniQMT、不下单。", confirmation: null },
  live_cert_reset: { title: "撤销实盘认证", text: "将归档并撤销实盘快速证书及其证据。撤销后如需重新启用实盘，必须重新执行1000元GC001实盘认证（真实买入），请确认后再操作。", confirmation: "REVOKE LIVE CERT", typed: true },
  mail_test: { title: "发送测试邮件", text: "将使用本机已保存的加密SMTP配置发送一封测试邮件。", confirmation: null },
  wx_test: { title: "发送测试微信通知", text: "将使用本机已保存的加密WxPusher配置发送一条测试通知到你的微信。", confirmation: null },
};

async function runAction(action) {
  if (busy) return;
  const detail = actionDetails[action];
  if (!detail) return;
  if (action === "live_cert") {
    setBusy(true, "正在执行实盘只读预检；不会下单…");
    try {
      const preflight = await api("/api/action", {
        method: "POST",
        body: JSON.stringify({ action: "live_cert_preflight", confirmation: null }),
      });
      if (!preflight.ok) throw new Error(preflight.output || "只读预检失败");
      elements.operationOutput.textContent = preflight.output;
    } catch (error) {
      const detail = String(error.message || error);
      if (detail.includes("A live-channel certificate already exists")) {
        elements.operationOutput.textContent =
          "已存在实盘认证证书。如需重新认证，请先点击“撤销实盘认证”并输入 REVOKE LIVE CERT，然后再次执行认证。";
      } else {
        elements.operationOutput.textContent = `只读预检失败；没有下单\n${detail}`;
      }
      setBusy(false);
      return;
    }
    setBusy(false);
  }
  const accepted = await confirmAction(
    detail.title,
    detail.text,
    detail.typed ? detail.confirmation : null,
  );
  if (!accepted) return;
  setBusy(true, `${detail.title}…`);
  try {
    const payload = await api("/api/action", {
      method: "POST",
      body: JSON.stringify({
        action,
        confirmation: detail.typed ? accepted : detail.confirmation,
      }),
    });
    if (!payload.ok) throw new Error(payload.output || "命令执行失败");
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

async function closeUi() {
  if (busy) return;
  const accepted = await confirmAction(
    "关闭本机控制台",
    "只有后台操作全部处于Idle时才能关闭。成功后会结束 rr ui 后台命令并尝试关闭本页。",
  );
  if (!accepted) return;
  setBusy(true, "正在确认后台操作均为Idle并关闭控制台…");
  try {
    await api("/api/shutdown", {
      method: "POST",
      body: JSON.stringify({ confirmation: "CLOSE UI" }),
    });
    sessionStorage.removeItem("rrLocalToken");
    document.body.replaceChildren();
    const closed = document.createElement("main");
    closed.className = "closed-screen";
    const title = document.createElement("h1");
    title.textContent = "控制台已安全关闭";
    const message = document.createElement("p");
    message.textContent = "后台操作均为Idle，rr ui命令已经结束。若本页没有自动关闭，现在可以直接关闭。";
    closed.append(title, message);
    document.body.append(closed);
    window.setTimeout(() => {
      window.close();
      window.setTimeout(() => window.location.replace("about:blank"), 200);
    }, 150);
  } catch (error) {
    elements.operationOutput.textContent = `暂时不能关闭\n${String(error.message || error)}`;
    setBusy(false);
  }
}

elements.refresh.addEventListener("click", refresh);
elements.closeUi.addEventListener("click", closeUi);
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
