/* BoxPorter console: vanilla JS. The browser is only a control surface:
   all state comes from the server; buttons send idempotent commands. */

const CLIENT_HEADER = { "X-BoxPorter-Client": "console", "Content-Type": "application/json" };
let currentProjectId = null;
const clientSessionId = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
let lastEventSeq = parseInt(sessionStorage.getItem("boxporter.lastEventSeq") || "0", 10);
function idemKey(action, target) {
  return `${clientSessionId}-${action}-${target || "x"}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const structured = data.error || data;
    const message = structured.message || data.detail || data.error || `HTTP ${response.status}`;
    const error = new Error(typeof message === "string" ? message : JSON.stringify(message));
    error.code = structured.code || data.code;
    error.hint = structured.hint || data.hint;
    error.field = structured.field;
    error.traceId = structured.trace_id || data.trace_id;
    throw error;
  }
  return data;
}

function post(path, body, idemKeyValue) {
  const headers = { ...CLIENT_HEADER };
  if (idemKeyValue) headers["Idempotency-Key"] = idemKeyValue;
  return api(path, { method: "POST", headers, body: JSON.stringify(body || {}) });
}

function postForm(path, formData, idemKeyValue) {
  const headers = { "X-BoxPorter-Client": "console" };
  if (idemKeyValue) headers["Idempotency-Key"] = idemKeyValue;
  return api(path, { method: "POST", headers, body: formData });
}

function switchView(name) {
  document.querySelectorAll("#app-view > section").forEach(s => s.classList.add("hidden"));
  document.getElementById(`view-${name}`).classList.remove("hidden");
  document.querySelectorAll("header nav button[data-view]").forEach(b =>
    b.classList.toggle("active", b.dataset.view === name));
  if (name === "dashboard") loadDashboard();
  if (name === "tasks") loadTasks();
  if (name === "approvals") loadApprovals();
  if (name === "system") loadSystem();
  if (name === "sessions") { loadSessions(); loadMode(); }
}

function showApp() {
  document.getElementById("login-view").classList.add("hidden");
  document.getElementById("app-view").classList.remove("hidden");
  document.getElementById("topbar").classList.remove("hidden");
  loadProjects();
  loadMode();
  loadHealth();
  switchView("dashboard");
  startEventStream();
}

async function loadProjects() {
  const projects = await api("/api/projects");
  const selector = document.getElementById("project-selector");
  selector.innerHTML = "";
  for (const project of projects.projects) {
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = `${project.name}（${project.id}）`;
    selector.appendChild(option);
  }
  if (projects.projects.length && !currentProjectId) {
    currentProjectId = projects.projects[0].id;
    selector.value = currentProjectId;
  }
  selector.onchange = () => {
    currentProjectId = selector.value;
    loadDashboard();
    loadTasks();
  };
  const taskSelect = document.getElementById("task-project-select");
  taskSelect.innerHTML = "";
  for (const project of projects.projects) {
    const option = document.createElement("option");
    option.value = project.id;
    option.textContent = project.id;
    taskSelect.appendChild(option);
  }
  if (currentProjectId) taskSelect.value = currentProjectId;
}

function renderBox(box, items) {
  const boxNames = { PENDING: "待处理箱", ACTIVE: "处理中箱", BLOCKED: "阻塞箱", PASSED: "已通过箱", ARCHIVED: "归档" };
  const element = document.createElement("div");
  element.className = "box";
  const header = document.createElement("h3");
  header.textContent = boxNames[box] || box;
  const count = document.createElement("span");
  count.className = "count";
  count.textContent = items.length;
  header.appendChild(count);
  element.appendChild(header);
  for (const item of items) {
    const card = document.createElement("div");
    card.className = "card";
    card.textContent = item.title;
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `${item.task_id} · ${item.state} · risk:${item.risk_level}`;
    card.appendChild(meta);
    card.onclick = () => openTask(item.task_id);
    element.appendChild(card);
  }
  return element;
}

async function loadDashboard() {
  try {
    if (!currentProjectId) {
      await loadProjects();
      if (!currentProjectId) return;
    }
    const dashboard = await api(`/api/projects/${currentProjectId}/dashboard`);
    const boxes = document.getElementById("boxes");
    boxes.innerHTML = "";
    for (const [box, items] of Object.entries(dashboard.boxes)) {
      boxes.appendChild(renderBox(box, items));
    }
    const blocked = dashboard.boxes.BLOCKED || [];
    const needs = document.getElementById("needs-user-list");
    needs.innerHTML = "";
    for (const item of blocked) {
      const li = document.createElement("li");
      li.textContent = `${item.task_id} ${item.title}`;
      const unblock = document.createElement("button");
      unblock.textContent = "解阻塞";
      unblock.onclick = () => post(`/api/tasks/${item.task_id}/unblock`).then(() => loadDashboard()).catch(showError);
      li.appendChild(unblock);
      needs.appendChild(li);
    }
  } catch (error) { showError(error.message); }
}

async function loadTasks() {
  const query = currentProjectId ? `?project_id=${encodeURIComponent(currentProjectId)}` : "";
  const data = await api(`/api/tasks${query}`);
  const list = document.getElementById("task-list");
  list.innerHTML = "";
  const recent = document.getElementById("recent-task-list");
  recent.innerHTML = "";
  const sorted = [...data.tasks].sort((a, b) =>
    (b.created_at || "").localeCompare(a.created_at || ""));
  sorted.slice(0, 3).forEach(task => {
    const card = document.createElement("div");
    card.className = "card";
    card.textContent = task.title;
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `${task.id} · ${task.state} · ${(task.created_at || "").slice(0, 16)}`;
    card.appendChild(meta);
    card.onclick = () => openTask(task.id);
    recent.appendChild(card);
  });
  for (const task of data.tasks) {
    const card = document.createElement("div");
    card.className = "card";
    card.textContent = task.title;
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `${task.id} · ${task.state} · attempt ${task.current_attempt}`;
    card.appendChild(meta);
    card.onclick = () => openTask(task.id);
    list.appendChild(card);
  }
}

function actionButton(label, fn, highRisk = false) {
  const button = document.createElement("button");
  button.textContent = label;
  button.onclick = async () => {
    try {
      await fn();
      loadTasks();
    } catch (error) {
      if (highRisk && error.message.includes("重新认证")) {
        switchView("sessions");
      }
      showError(error.message);
    }
  };
  return button;
}

async function openTask(taskId) {
  const data = await api(`/api/tasks/${taskId}`);
  const detail = document.getElementById("task-detail");
  const task = data.task;
  const actions = document.createElement("div");
  actions.className = "actions";
  if (task.state === "PENDING") actions.appendChild(actionButton("就绪", () => post(`/api/tasks/${taskId}/ready`, {}, idemKey("ready", taskId))));
  if (["PENDING", "READY", "WORKING", "REVISE", "BLOCKED", "FAILED"].includes(task.state)) {
    actions.appendChild(actionButton("取消", () => post(`/api/tasks/${taskId}/cancel`, {}, idemKey("cancel", taskId))));
  }
  if (task.state === "FAILED" || task.state === "REVISE") {
    actions.appendChild(actionButton("重试", () => post(`/api/tasks/${taskId}/retry`, {}, idemKey("retry", taskId))));
  }
  if (task.state === "WORKING") {
    actions.appendChild(actionButton("阻塞", () => post(`/api/tasks/${taskId}/block`, { reason: prompt("阻塞原因") }, idemKey("block", taskId))));
  }
  detail.innerHTML = "";
  const heading = document.createElement("h3");
  heading.textContent = `${task.id} ${task.title} [${task.state} / ${task.box}]`;
  detail.appendChild(heading);
  detail.appendChild(actions);

  const tabs = document.createElement("div");
  tabs.className = "detail-tabs";
  for (const [tabId, label] of [["overview", "概览"], ["runs", "运行"], ["events", "事件"], ["submission", "提交包"], ["review", "审核"]]) {
    const button = document.createElement("button");
    button.textContent = label;
    button.dataset.tab = tabId;
    button.onclick = () => renderTaskTab(data, taskId, tabId);
    tabs.appendChild(button);
  }
  detail.appendChild(tabs);
  const tabBody = document.createElement("div");
  tabBody.id = "task-tab-body";
  detail.appendChild(tabBody);
  renderTaskTab(data, taskId, "overview");
}

function renderTaskTab(data, taskId, tabId) {
  document.querySelectorAll("#task-detail .detail-tabs button").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === tabId));
  const body = document.getElementById("task-tab-body");
  body.innerHTML = "";
  const task = data.task;
  if (tabId === "overview") {
    const objective = document.createElement("p");
    objective.textContent = task.objective;
    body.appendChild(objective);
    const criteria = document.createElement("ul");
    for (const item of task.acceptance_criteria) {
      const li = document.createElement("li");
      li.textContent = item;
      criteria.appendChild(li);
    }
    body.appendChild(criteria);
    const readinessBox = document.createElement("div");
    readinessBox.className = "card";
    readinessBox.innerHTML = "<h4>启动条件检查</h4><div class='hint'>检查中…</div>";
    body.appendChild(readinessBox);
    api(`/api/tasks/${taskId}/readiness`).then(r => {
      const gapDiv = readinessBox.querySelector(".hint");
      if (r.ready) {
        gapDiv.className = "gap-ok";
        gapDiv.textContent = "✓ 已满足 READY 条件，可由调度器启动执行";
      } else {
        gapDiv.className = "error-list";
        gapDiv.innerHTML = "";
        const ul = document.createElement("ul");
        for (const gap of r.gaps) {
          const li = document.createElement("li");
          li.textContent = `${gap.field || "spec"}: ${gap.message}`;
          if (gap.hint) {
            const hint = document.createElement("div");
            hint.className = "hint";
            hint.textContent = `→ ${gap.hint}`;
            li.appendChild(hint);
          }
          ul.appendChild(li);
        }
        gapDiv.appendChild(ul);
      }
    }).catch(() => {});
  } else if (tabId === "runs") {
    const table = document.createElement("table");
    table.innerHTML = "<tr><th>Run</th><th>角色</th><th>Runner</th><th>状态</th><th>Session</th><th>操作</th></tr>";
    for (const run of data.runs) {
      const row = document.createElement("tr");
      row.innerHTML = `<td>${run.id.slice(-8)}</td><td>${run.role}</td><td>${run.runner}</td><td>${run.state}</td><td>${run.session_id}</td>`;
      const cell = document.createElement("td");
      const open = document.createElement("button");
      open.textContent = "运行页";
      open.onclick = () => openRun(run.id, taskId);
      cell.appendChild(open);
      row.appendChild(cell);
      table.appendChild(row);
    }
    body.appendChild(table);
  } else if (tabId === "events") {
    const pre = document.createElement("pre");
    pre.id = "event-log-detail";
    pre.textContent = data.events.map(e => `${e.seq} ${e.occurred_at} ${e.event_type} ${JSON.stringify(e.payload)}`).join("\n");
    body.appendChild(pre);
  } else if (tabId === "submission") {
    if (!data.submission) {
      body.innerHTML = "<div class='hint'>暂无提交包</div>";
      return;
    }
    const info = document.createElement("div");
    info.className = "card";
    info.innerHTML =
      `<h4>提交包</h4>` +
      `<div class='meta'>submission_sha256: ${data.submission.submission_sha256}</div>` +
      `<div class='meta'>head_commit: ${data.submission.head_commit}</div>` +
      `<div class='meta'>frozen_at: ${data.submission.frozen_at}</div>` +
      `<div class='meta'>invalidated: ${data.submission.invalidated}</div>`;
    body.appendChild(info);
  } else if (tabId === "review") {
    if (!data.reviews.length) {
      body.innerHTML = "<div class='hint'>暂无审核记录</div>";
      return;
    }
    const table = document.createElement("table");
    table.innerHTML = "<tr><th>结果</th><th>Run</th><th>证据 sha</th><th>时间</th></tr>";
    for (const review of data.reviews) {
      const row = document.createElement("tr");
      row.innerHTML = `<td>${review.result}</td><td>${review.run_id.slice(-8)}</td><td>${(review.evidence_sha256 || "").slice(0, 12)}</td><td>${review.created_at}</td>`;
      table.appendChild(row);
    }
    body.appendChild(table);
  }
}

async function openRun(runId, backTaskId) {
  const detail = document.getElementById("task-detail");
  detail.innerHTML = "";
  const back = document.createElement("button");
  back.textContent = "← 返回任务";
  back.onclick = () => openTask(backTaskId);
  detail.appendChild(back);
  const box = document.createElement("div");
  box.id = "run-detail";
  detail.appendChild(box);
  const runData = await api(`/api/runs/${runId}`);
  const run = runData.run;
  const heading = document.createElement("h3");
  heading.textContent = `Run ${run.id.slice(-8)} · ${run.role} / ${run.runner} [${run.state}]`;
  box.appendChild(heading);
  const meta = document.createElement("div");
  meta.className = "card";
  meta.innerHTML =
    `<div class='meta'>task: ${runData.task_id} · attempt: ${runData.attempt}</div>` +
    `<div class='meta'>session: ${run.session_id} · identity: ${run.identity}</div>` +
    `<div class='meta'>worktree: ${run.worktree || "-"} · prompt_sha: ${(run.prompt_sha || "").slice(0, 12)}</div>` +
    `<div class='meta'>stop_reason: ${run.stop_reason || "-"}</div>` +
    (runData.lease
      ? `<div class='meta'>lease: token ${runData.lease.fencing_token} · 心跳 ${runData.lease.heartbeat_at} · 到期 ${runData.lease.expires_at}</div>`
      : "<div class='meta'>lease: 无</div>");
  box.appendChild(meta);
  const actions = document.createElement("div");
  actions.className = "actions";
  const stopBtn = document.createElement("button");
  stopBtn.textContent = "停止";
  stopBtn.onclick = () => post(`/api/runs/${runId}/stop`, {}, idemKey("stop-run", runId)).then(openRun.bind(null, runId, backTaskId)).catch(e => showError(e.message));
  actions.appendChild(stopBtn);
  const resumeBtn = document.createElement("button");
  resumeBtn.textContent = "续跑";
  resumeBtn.onclick = async () => {
    try {
      const result = await post(`/api/runs/${runId}/resume`, {}, idemKey("resume-run", runId));
      await openRun(runId, backTaskId);
      showError(result.message || "已续跑");
    } catch (error) {
      showError(`${error.message} → ${error.hint || ""}`);
    }
  };
  actions.appendChild(resumeBtn);
  box.appendChild(actions);
  const status = document.createElement("div");
  status.className = "hint";
  status.textContent = "事件流已连接";
  box.appendChild(status);
  const pre = document.createElement("pre");
  pre.id = "event-log-detail";
  box.appendChild(pre);
  let cursor = 0;
  const poll = async () => {
    try {
      const events = await api(`/api/runs/${runId}/events?after_cursor=${cursor}`);
      for (const event of events.events) {
        pre.textContent += `${event.seq} ${event.occurred_at} ${event.event_type} ${JSON.stringify(event.payload)}\n`;
        cursor = Math.max(cursor, event.seq);
      }
      pre.scrollTop = pre.scrollHeight;
    } catch (error) {
      status.textContent = `事件流异常：${error.message}`;
    }
  };
  await poll();
  const timer = setInterval(poll, 3000);
  window.addEventListener("beforeunload", () => clearInterval(timer), { once: true });
}

async function loadApprovals() {
  try {
    const data = await api("/api/approvals");
    const list = document.getElementById("approval-list");
    list.innerHTML = "";
    for (const approval of data.approvals) {
      const card = document.createElement("div");
      card.className = "card";
      card.textContent = `${approval.action} → ${approval.target}`;
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = `风险:${approval.risk_level} · 状态:${approval.status} · 到期:${approval.expires_at} · 次数:${approval.used_count}/${approval.max_uses}`;
      card.appendChild(meta);
      if (approval.status === "pending") {
        const approve = document.createElement("button");
        approve.textContent = "批准";
        approve.onclick = () => post(`/api/approvals/${approval.id}/approve`, {}, idemKey("approve", approval.id))
          .then(loadApprovals)
          .catch(e => { switchView("sessions"); showError(e.message); });
        const reject = document.createElement("button");
        reject.textContent = "拒绝";
        reject.onclick = () => post(`/api/approvals/${approval.id}/reject`, {}, idemKey("reject", approval.id))
          .then(loadApprovals)
          .catch(e => { switchView("sessions"); showError(e.message); });
        card.appendChild(approve);
        card.appendChild(reject);
      }
      list.appendChild(card);
    }
    // 阻塞看板
    const blockers = await api("/api/blockers");
    const blockerList = document.getElementById("blocker-list");
    blockerList.innerHTML = "";
    for (const blocker of blockers.blockers) {
      const card = document.createElement("div");
      card.className = "card";
      card.textContent = `${blocker.task_id}${blocker.task_title ? " · " + blocker.task_title : ""}`;
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = `${blocker.reason} · 探针:${blocker.probe_command.length ? "有" : "无"} · 下次探测:${blocker.next_probe_at || "-"}`;
      card.appendChild(meta);
      const unblock = document.createElement("button");
      unblock.textContent = "解阻塞";
      unblock.onclick = () => post(`/api/tasks/${blocker.task_id}/unblock`, {}, idemKey("unblock", blocker.task_id))
        .then(loadApprovals)
        .catch(e => showError(e.message));
      card.appendChild(unblock);
      blockerList.appendChild(card);
    }
  } catch (error) { showError(error.message); }
}

function setMode(mode) {
  post("/api/settings/mode", { mode })
    .then(() => {
      document.getElementById("mode-status").textContent = `已切换：${mode}`;
      loadMode();
    })
    .catch(e => {
      document.getElementById("mode-status").textContent = `需要重新认证：${e.message}`;
      switchView("sessions");
    });
}

function showError(message) {
  const element = document.getElementById("login-error");
  element.textContent = message;
  element.classList.remove("hidden");
  element.parentElement && element.parentElement.classList.contains("hidden") &&
    document.getElementById("event-log") &&
    (document.getElementById("event-log").textContent += `\n[错误] ${message}\n`);
}

/* ---- 新建任务面板（remediation 里程碑 A） ---- */

const REQUIRED_FIELDS = ["task_id", "project_id", "title", "objective", "workspace", "acceptance_criteria"];

function validateSpec(value) {
  const errors = [];
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return [{ field: null, message: "spec 顶层必须是 JSON 对象" }];
  }
  for (const field of REQUIRED_FIELDS) {
    if (value[field] === undefined || value[field] === null || value[field] === "") {
      errors.push({ field, message: `缺少必填字段 ${field}` });
    }
  }
  if (Array.isArray(value.acceptance_criteria) && value.acceptance_criteria.length === 0) {
    errors.push({ field: "acceptance_criteria", message: "acceptance_criteria 不能为空数组" });
  }
  for (const field of ["title", "objective"]) {
    if (typeof value[field] === "string" && value[field].length > 2000) {
      errors.push({ field, message: `${field} 超过 2000 字符上限` });
    }
  }
  if (value.task_id && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(value.task_id)) {
    errors.push({ field: "task_id", message: "task_id 格式非法（字母数字开头，≤128 字符）" });
  }
  return errors;
}

function renderErrorList(container, errors) {
  container.innerHTML = "";
  const ul = document.createElement("ul");
  ul.className = "error-list";
  for (const item of errors) {
    const li = document.createElement("li");
    li.textContent = `${item.field || "spec"}: ${item.message}`;
    if (item.hint) {
      const hint = document.createElement("div");
      hint.className = "hint";
      hint.textContent = `→ ${item.hint}`;
      li.appendChild(hint);
    }
    ul.appendChild(li);
  }
  container.appendChild(ul);
}

function showTaskModal() {
  document.getElementById("task-modal").classList.remove("hidden");
  document.getElementById("client-errors").innerHTML = "";
  document.getElementById("server-result").innerHTML = "";
  document.getElementById("task-success-actions").classList.add("hidden");
  document.getElementById("task-create-submit").disabled = false;
}

function hideTaskModal() {
  document.getElementById("task-modal").classList.add("hidden");
}

async function submitTaskCreation() {
  const isFileTab = !document.getElementById("task-spec-file").classList.contains("hidden");
  const errorBox = document.getElementById("client-errors");
  const resultBox = document.getElementById("server-result");
  const submit = document.getElementById("task-create-submit");
  const projectId = document.getElementById("task-project-select").value;
  let text = "";
  if (isFileTab) {
    const fileInput = document.getElementById("task-spec-file");
    if (!fileInput.files || !fileInput.files[0]) {
      renderErrorList(errorBox, [{ field: "file", message: "请选择 .json 文件" }]);
      return;
    }
    text = await fileInput.files[0].text();
  } else {
    text = document.getElementById("task-spec-text").value;
  }
  if (!text.trim()) {
    renderErrorList(errorBox, [{ field: null, message: "请输入或选择 spec 内容" }]);
    return;
  }
  let value;
  try {
    value = JSON.parse(text);
  } catch (error) {
    renderErrorList(errorBox, [{ field: "json", message: `JSON 解析失败: ${error.message}`, hint: "检查引号/逗号/括号配对" }]);
    return;
  }
  if (projectId && !value.project_id) value.project_id = projectId;
  const clientErrors = validateSpec(value);
  if (clientErrors.length) {
    renderErrorList(errorBox, clientErrors);
    return;
  }
  errorBox.innerHTML = "";
  resultBox.innerHTML = "";
  submit.disabled = true;
  submit.textContent = "创建中…";
  try {
    // 统一走 /api/tasks/import：文件与文本共享后端解码、校验与错误模型。
    const form = new FormData();
    if (isFileTab) {
      form.append("file", document.getElementById("task-spec-file").files[0]);
    } else {
      form.append("spec_json", text);
    }
    if (projectId) form.append("project_id", projectId);
    const result = await postForm("/api/tasks/import", form, idemKey("create", value.task_id));
    submit.textContent = "创建任务";
    submit.disabled = false;
    resultBox.className = "gap-ok";
    resultBox.textContent = `✓ 已创建 ${result.data.task_id}（状态 ${result.data.state}）· trace ${result.trace_id || "-"} · 重复提交防护已启用`;
    const actions = document.getElementById("task-success-actions");
    actions.classList.remove("hidden");
    actions.innerHTML = "";
    const readyBtn = document.createElement("button");
    readyBtn.textContent = "标记 READY";
    readyBtn.onclick = async () => {
      try {
        await post(`/api/tasks/${result.data.task_id}/ready`, {}, idemKey("ready", result.data.task_id));
        actions.textContent = "已 READY，调度器可自动启动执行";
      } catch (error) {
        renderErrorList(errorBox, [{ field: error.field, message: error.message, hint: error.hint }]);
      }
    };
    const viewBtn = document.createElement("button");
    viewBtn.textContent = "查看详情";
    viewBtn.onclick = () => {
      hideTaskModal();
      switchView("tasks");
      openTask(result.data.task_id);
    };
    actions.appendChild(readyBtn);
    actions.appendChild(viewBtn);
    loadTasks();
  } catch (error) {
    submit.textContent = "创建任务";
    submit.disabled = false;
    renderErrorList(errorBox, [{ field: error.field, message: error.message, hint: error.hint }]);
  }
}

/* ---- 系统视图：Runner 能力矩阵（remediation 里程碑 B） ---- */

async function loadSystem() {
  const panel = document.getElementById("health-panel");
  panel.innerHTML = "<div class='hint'>加载中…</div>";
  try {
    const health = await api("/api/system/health");
    panel.innerHTML = "";
    const grid = document.createElement("div");
    grid.className = "health-grid";
    for (const component of Object.values(health.components || {})) {
      const item = document.createElement("div");
      item.className = "health-item";
      const status = document.createElement("span");
      status.className = `status status-${component.status}`;
      status.textContent = component.status;
      item.textContent = component.name;
      item.appendChild(status);
      if (component.detail) {
        const detail = document.createElement("div");
        detail.className = "hint";
        detail.textContent = typeof component.detail === "string"
          ? component.detail
          : JSON.stringify(component.detail);
        item.appendChild(detail);
      }
      grid.appendChild(item);
    }
    panel.appendChild(grid);
    const warnings = health.warnings || [];
    const badge = document.getElementById("health-badge");
    badge.textContent = warnings.length ? `健康 ⚠ ${warnings.length}` : "健康";
    badge.className = warnings.length ? "badge warn" : "badge ok";
  } catch (error) {
    panel.innerHTML = `<div class='error-list'>${error.message}</div>`;
  }

  const box = document.getElementById("runner-capabilities");
  box.innerHTML = "<div class='hint'>加载中…</div>";
  try {
    const data = await api("/api/system/runners");
    box.innerHTML = "";
    if (!data.runners || !data.runners.length) {
      box.innerHTML = "<div class='hint'>未注册任何 Runner：daemon 将拒绝调度，请通过环境变量配置（见 docs/operations/README.md）</div>";
      return;
    }
    for (const runner of data.runners) {
      const card = document.createElement("div");
      card.className = "card";
      card.textContent = `${runner.name} · v${runner.version}`;
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.innerHTML =
        `checkpoint: <span class='${runner.supports_checkpoint ? "cap-yes" : "cap-no"}'>${runner.supports_checkpoint ? "支持" : "不支持"}</span> · ` +
        `resume: <span class='${runner.supports_resume ? "cap-yes" : "cap-no"}'>${runner.supports_resume ? "支持" : "不支持"}</span> · ` +
        `model: ${runner.requires_model ? "需要模型" : "零模型"}`;
      if (!runner.supports_resume) {
        const note = document.createElement("div");
        note.className = "hint";
        note.textContent = "恢复边界：中断后不会继续同一会话；失败后按恢复预算走新 Attempt（ADR-015）";
        meta.appendChild(note);
      }
      card.appendChild(meta);
      box.appendChild(card);
    }
  } catch (error) {
    box.innerHTML = `<div class='error-list'>${error.message}</div>`;
  }
}

async function loadMode() {
  try {
    const data = await api("/api/settings/mode");
    const badge = document.getElementById("mode-badge");
    badge.textContent = `策略 ${data.mode}`;
  } catch { /* ignore */ }
}

async function loadHealth() {
  try {
    const data = await api("/api/system/health");
    const badge = document.getElementById("health-badge");
    badge.textContent = data.ok ? "健康" : "异常";
    badge.className = data.ok ? "badge ok" : "badge warn";
  } catch { /* ignore */ }
}

async function loadSessions() {
  const data = await api("/api/auth/sessions");
  const list = document.getElementById("session-list");
  list.innerHTML = "";
  for (const session of data.sessions) {
    const card = document.createElement("div");
    card.className = "card";
    card.textContent = `${session.device_label}${session.current ? "（当前设备）" : ""}`;
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `最近活动 ${session.last_seen_at}`;
    card.appendChild(meta);
    if (!session.current) {
      const revoke = document.createElement("button");
      revoke.textContent = "撤销";
      revoke.onclick = () => post(`/api/auth/sessions/${session.id}/revoke`).then(loadSessions).catch(e => showError(e.message));
      card.appendChild(revoke);
    }
    list.appendChild(card);
  }
}

function startEventStream() {
  const source = new EventSource(`/api/events/stream?after_cursor=${lastEventSeq}`);
  const badge = document.getElementById("conn-badge");
  const log = document.getElementById("event-log");
  source.onopen = () => {
    badge.textContent = "已连接";
    badge.className = "badge ok";
  };
  source.onerror = () => {
    badge.textContent = "重连中…";
    badge.className = "badge warn";
  };
  source.addEventListener("boxporter", async event => {
    const record = JSON.parse(event.data);
    // 断线缺口检测：游标跳号时从服务端按 seq 补齐回放（ADR-013）。
    if (record.seq > lastEventSeq + 1) {
      const backfill = await api(`/api/events?after_cursor=${lastEventSeq}`).catch(() => null);
      if (backfill) {
        for (const missed of backfill.events.filter(e => e.seq < record.seq)) {
          log.textContent += `${missed.seq} ${missed.occurred_at} ${missed.event_type} ${JSON.stringify(missed.payload)}\n`;
          lastEventSeq = Math.max(lastEventSeq, missed.seq);
        }
      }
    }
    if (record.seq > lastEventSeq) {
      log.textContent += `${record.seq} ${record.occurred_at} ${record.event_type} ${JSON.stringify(record.payload)}\n`;
      lastEventSeq = record.seq;
      sessionStorage.setItem("boxporter.lastEventSeq", String(lastEventSeq));
    }
    log.scrollTop = log.scrollHeight;
  });
}

async function login(event) {
  event.preventDefault();
  try {
    await api("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: "admin", password: document.getElementById("login-password").value }),
    });
    showApp();
  } catch (error) {
    showError(error.message);
  }
}

document.getElementById("login-form").addEventListener("submit", login);
document.getElementById("logout").onclick = () => {
  post("/api/auth/logout").catch(() => {});
  location.reload();
};
document.getElementById("reauth-submit").onclick = async () => {
  try {
    await post("/api/auth/reauthenticate", { password: document.getElementById("reauth-password").value });
    document.getElementById("reauth-status").textContent = "已认证";
  } catch (error) {
    document.getElementById("reauth-status").textContent = error.message;
  }
};
document.getElementById("events-clear").onclick = () => {
  document.getElementById("event-log").textContent = "";
};
document.getElementById("new-task-btn").onclick = showTaskModal;
document.getElementById("task-modal-close").onclick = hideTaskModal;
document.getElementById("tab-json").onclick = () => {
  document.getElementById("tab-json").classList.add("active");
  document.getElementById("tab-file").classList.remove("active");
  document.getElementById("task-spec-text").classList.remove("hidden");
  document.getElementById("task-spec-file").classList.add("hidden");
};
document.getElementById("tab-file").onclick = () => {
  document.getElementById("tab-file").classList.add("active");
  document.getElementById("tab-json").classList.remove("active");
  document.getElementById("task-spec-text").classList.add("hidden");
  document.getElementById("task-spec-file").classList.remove("hidden");
};
document.getElementById("task-create-submit").onclick = submitTaskCreation;
document.querySelectorAll("#mode-box button[data-mode]").forEach(button =>
  button.onclick = () => setMode(button.dataset.mode));
document.querySelectorAll("header nav button[data-view]").forEach(button =>
  button.onclick = () => switchView(button.dataset.view));
