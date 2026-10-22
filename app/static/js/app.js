import { api } from "./api.js";
import { fmtDuration, fmtTime, h, plural, safeHttpUrl, storage, toast, tokens, uuid } from "./dom.js";
import {
  ATTEMPT_STATUS, BEHAVIOURS, LEDGER_KIND, SCENARIOS, TASK_STATUS, errorText, eventText, reasonText,
} from "./labels.js";

const POLL_MS = 1500;
const $ = (id) => document.getElementById(id);

const state = {
  userId: null,
  models: [],
  model: "image-fast",
  behaviour: { alpha: "ok", beta: "ok" },
  expanded: new Set(),
  details: new Map(),
  lastRequest: null,
  balance: null,
  pollTimer: null,
};

/* ---------- theme ---------- */

function applyTheme(theme) {
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}

function initTheme() {
  applyTheme(storage.get("theme"));
  $("theme").addEventListener("click", () => {
    const order = [null, "dark", "light"];
    const next = order[(order.indexOf(storage.get("theme")) + 1) % order.length];
    storage.set("theme", next ?? "");
    applyTheme(next);
    toast(next ? `Тема: ${next === "dark" ? "тёмная" : "светлая"}` : "Тема: как в системе");
  });
}

/* ---------- user ---------- */

function randomTelegramId() {
  const [n] = crypto.getRandomValues(new Uint32Array(1));
  return String(100_000_000 + (n % 8_900_000_000));
}

function setUser(id) {
  state.userId = id;
  state.expanded.clear();
  state.details.clear();
  state.balance = null;
  state.lastRequest = null;
  $("repeat").disabled = true;
  $("user-id").value = id;
  storage.set("userId", id);
  refresh();
}

function initUser() {
  const saved = storage.get("userId");
  $("user-id").addEventListener("change", (e) => {
    const value = e.target.value.trim();
    if (/^[1-9]\d{0,15}$/.test(value)) setUser(value);
    else {
      toast("ID — положительное целое число", "bad");
      e.target.value = state.userId;
    }
  });
  $("user-random").addEventListener("click", () => {
    setUser(randomTelegramId());
    toast("Новый пользователь: баланс 0, задач нет");
  });
  setUser(saved && /^[1-9]\d{0,15}$/.test(saved) ? saved : randomTelegramId());
}

/* ---------- balance & ledger ---------- */

function renderBalance(data) {
  const value = $("balance");
  const changed = state.balance !== null && state.balance !== data.balance;
  value.textContent = data.balance;
  if (changed) {
    value.classList.remove("bump");
    void value.offsetWidth; // restart the animation
    value.classList.add("bump");
  }
  state.balance = data.balance;

  const box = $("ledger");
  if (!data.ledger.length) {
    box.replaceChildren(h("p", { class: "empty" }, "Операций пока нет"));
    return;
  }
  box.replaceChildren(...data.ledger.map((row) => {
    const sign = row.amount > 0 ? "plus" : "minus";
    const ref = row.payment_id ? `платёж ${row.payment_id.slice(0, 14)}…` : `задача ${row.task_id.slice(0, 8)}`;
    return h("div", { class: "ledger-row" },
      h("span", { class: "ledger-kind" }, LEDGER_KIND[row.kind] ?? row.kind),
      h("span", { class: `ledger-amount ${sign}` }, `${row.amount > 0 ? "+" : ""}${row.amount}`),
      h("span", { class: "ledger-meta" }, `${fmtTime(row.created_at)} · ${ref}`),
      h("span", { class: "ledger-after" }, `= ${row.balance_after}`),
    );
  }));
}

function initTopup() {
  $("topup").addEventListener("submit", async (e) => {
    e.preventDefault();
    const amount = Number($("topup-amount").value);
    const copies = Number($("topup-copies").value);
    const before = state.balance ?? 0;
    try {
      const { data } = await api.topup({ user_id: Number(state.userId), tokens: amount, copies });
      const fresh = (await api.balance(state.userId)).data;
      renderBalance(fresh);
      const codes = [...new Set(data.delivered)].join(", ");
      const credited = fresh.balance - before;
      toast(`Платёж отправлен ${copies} ${plural(copies, "вебхуком", "вебхуками", "вебхуками")} (ответы: ${codes}). `
        + `Начислено ${credited > 0 ? "+" : ""}${credited} — ровно один раз.`, "ok", 8000);
    } catch (error) {
      toast(errorText(error), "bad");
    }
  });
}

/* ---------- generation form ---------- */

function providerNames() {
  const names = new Set();
  for (const model of state.models) for (const leg of model.legs) names.add(leg.provider);
  return [...names].sort();
}

function renderModels() {
  $("models").replaceChildren(...state.models.map((model) => {
    const legs = model.legs.flatMap((leg, i) => [
      i ? h("span", { class: "leg-arrow" }, "→") : null,
      h("span", { class: "leg" }, `${leg.provider} ≤${leg.max_ref_images}`,
        leg.skip_on_moderation ? h("span", { class: "leg-flag", title: "skip_on_moderation" }, " ⊘mod") : null),
    ]);
    return h("button", {
      type: "button", class: "model", role: "radio", "aria-checked": String(model.slug === state.model),
      onclick: () => { state.model = model.slug; renderModels(); updatePreview(); },
    },
    h("div", { class: "model-head" },
      h("span", { class: "model-name" }, model.slug),
      h("span", { class: "model-price" }, tokens(model.price))),
    h("div", { class: "legs" }, legs));
  }));
}

function renderProviders() {
  $("providers").replaceChildren(...providerNames().map((name) => {
    const select = h("select", { class: "input", id: `behaviour-${name}` },
      BEHAVIOURS.map((b) => h("option", { value: b.id, selected: b.id === state.behaviour[name] }, b.label)));
    select.addEventListener("change", () => { state.behaviour[name] = select.value; markScenario(); updatePreview(); });
    return h("div", { class: "provider-box" },
      h("label", { class: "field" }, h("span", { class: "field-label" }, `Провайдер ${name}`), select));
  }));
}

function directives() {
  const fast = $("fast").checked;
  return providerNames().flatMap((name) => {
    const behaviour = BEHAVIOURS.find((b) => b.id === state.behaviour[name]) ?? BEHAVIOURS[0];
    const made = behaviour.make(name);
    return fast && !behaviour.timed ? [...made, `@${name}:delay=1`] : made;
  });
}

function finalPrompt() {
  return [$("prompt").value.trim(), ...directives()].join(" ").trim();
}

function updatePreview() {
  $("final-prompt").textContent = finalPrompt();
  const model = state.models.find((m) => m.slug === state.model);
  $("submit").textContent = model ? `Сгенерировать · ${tokens(model.price)}` : "Сгенерировать";
}

function markScenario() {
  for (const chip of $("scenarios").children) {
    const s = SCENARIOS[Number(chip.dataset.index)];
    chip.classList.toggle("active", s.model === state.model
      && Object.entries(state.behaviour).every(([p, b]) => s[p] === b));
  }
}

function renderScenarios() {
  $("scenarios").replaceChildren(...SCENARIOS.map((s, index) => h("button", {
    type: "button", class: "chip", "data-index": index,
    onclick: () => {
      state.model = s.model;
      for (const name of providerNames()) {
        state.behaviour[name] = s[name] ?? "ok";
        $(`behaviour-${name}`).value = state.behaviour[name];
      }
      $("refs").value = 0;
      renderModels();
      markScenario();
      updatePreview();
    },
  }, s.label)));
  markScenario();
}

async function submitGenerate(body, key, isRepeat) {
  try {
    const { status, data } = await api.generate(body, key);
    state.expanded.add(data.task_id);
    if (isRepeat) {
      toast(status === 200
        ? "Тот же Idempotency-Key → та же задача, повторного списания нет"
        : "Создана новая задача", "ok");
    } else {
      toast(`Задача ${data.task_id.slice(0, 8)} создана, токены списаны`, "ok");
    }
    await refresh();
  } catch (error) {
    toast(errorText(error), "bad", 7000);
  }
}

function initGenerate() {
  $("prompt").addEventListener("input", updatePreview);
  $("fast").addEventListener("change", updatePreview);
  $("generate").addEventListener("submit", async (e) => {
    e.preventDefault();
    const refs = Math.max(0, Number($("refs").value) || 0);
    const body = {
      user_id: Number(state.userId),
      model_slug: state.model,
      prompt: finalPrompt(),
      ref_images: Array.from({ length: refs }, (_, i) => `https://picsum.photos/seed/ref${i + 1}/512`),
    };
    const key = uuid();
    state.lastRequest = { body, key };
    $("repeat").disabled = false;
    $("submit").disabled = true;
    try {
      await submitGenerate(body, key, false);
    } finally {
      $("submit").disabled = false;
    }
  });
  $("repeat").addEventListener("click", () => {
    if (state.lastRequest) submitGenerate(state.lastRequest.body, state.lastRequest.key, true);
  });
}

/* ---------- tasks ---------- */

function statusPill([text, cls]) {
  return h("span", { class: `pill ${cls}` }, h("span", { class: "dot" }), text);
}

function attemptDetail(a) {
  const parts = [];
  if (a.error) parts.push(`${a.error} — ${reasonText(a.reason)}`);
  else if (a.reason) parts.push(reasonText(a.reason));
  if (a.status === "succeeded" && a.reason === "watchdog_timeout") parts.push("результат пришёл после сторожа и принят");
  if (a.status === "sent" && a.deadline_at) {
    const left = Math.max(0, Math.round((new Date(a.deadline_at) - Date.now()) / 1000));
    parts.push(`сторож через ${left} с`);
  }
  parts.push(`отправлено ${fmtTime(a.sent_at) || "—"}`);
  if (a.finished_at) parts.push(`завершено ${fmtTime(a.finished_at)} (${fmtDuration(a.sent_at || a.created_at, a.finished_at)})`);
  return parts.join(" · ");
}

function renderTaskBody(task) {
  const body = h("div", { class: "task-body" });
  if (task.status === "succeeded") {
    const href = safeHttpUrl(task.result_url);
    body.append(h("div", { class: "result" },
      h("strong", {}, "Результат"),
      href ? h("a", { href, target: "_blank", rel: "noopener noreferrer" }, href) : task.result_url));
  } else if (task.status === "failed") {
    body.append(h("div", { class: "result refunded" },
      h("strong", {}, `Ошибка: ${task.error}`), h("span", {}, `${tokens(task.price)} возвращено`)));
  }
  body.append(h("div", { class: "attempts" }, task.attempts.map((a) => h("div", { class: "attempt" },
    h("span", { class: "attempt-num" }, a.number),
    h("span", { class: "attempt-provider" }, `${a.provider} · нога ${a.leg_priority}`),
    statusPill(ATTEMPT_STATUS[a.status] ?? [a.status, "pill-muted"]),
    h("span", { class: "attempt-detail" }, attemptDetail(a)),
  ))));
  body.append(h("details", { class: "events" },
    h("summary", {}, `Журнал событий (${task.events.length})`),
    h("ol", {}, task.events.map((e) => h("li", { class: e.kind.includes("ignored") ? "ignored" : "" },
      h("time", { datetime: e.created_at }, fmtTime(e.created_at)), eventText(e))))));
  body.append(h("p", { class: "hint mono" }, `task_id ${task.id}`));
  return body;
}

function renderTasks(tasks) {
  const box = $("tasks");
  if (!tasks.length) {
    box.replaceChildren(h("p", { class: "empty" }, "Задач пока нет — запустите генерацию"));
    return;
  }
  box.replaceChildren(...tasks.map((summary) => {
    const task = state.details.get(summary.id) ?? summary;
    const open = state.expanded.has(task.id);
    const leg = task.current_leg ? ` · ${task.current_leg.provider}` : "";
    const head = h("button", {
      type: "button", class: "task-head", "aria-expanded": String(open),
      onclick: () => {
        if (open) state.expanded.delete(task.id); else state.expanded.add(task.id);
        refresh();
      },
    },
    h("div", { class: "task-main" },
      h("div", { class: "task-title" }, h("span", { class: "mono" }, task.model), statusPill(TASK_STATUS[task.status])),
      h("div", { class: "task-sub" }, `${fmtTime(task.created_at)}${leg} · ${task.id.slice(0, 8)}`)),
    h("span", { class: "task-price", title: task.status === "failed" ? "списано и возвращено" : "списано" },
      task.status === "failed" ? "±0" : `−${task.price}`));
    return h("article", { class: "task" }, head, open && task.attempts ? renderTaskBody(task) : null);
  }));
}

/* ---------- refresh loop ---------- */

async function refresh() {
  clearTimeout(state.pollTimer);
  const userId = state.userId;
  try {
    const [balance, tasks] = await Promise.all([api.balance(userId), api.tasks(userId)]);
    if (userId !== state.userId) return; // the user was switched meanwhile
    renderBalance(balance.data);
    const wanted = tasks.data.filter((t) => state.expanded.has(t.id) || t.status === "in_progress");
    const details = await Promise.all(wanted.map((t) => api.task(t.id).then((r) => r.data)));
    for (const d of details) state.details.set(d.id, d);
    renderTasks(tasks.data);
    $("tasks-hint").textContent = tasks.data.some((t) => t.status === "in_progress") ? "обновляется само…" : "все задачи завершены";
  } catch (error) {
    $("tasks-hint").textContent = "нет связи с сервисом";
    if (error.status) toast(errorText(error), "bad");
  } finally {
    if (!document.hidden) state.pollTimer = setTimeout(refresh, POLL_MS);
  }
}

async function refreshInvariant() {
  const pill = $("invariant");
  try {
    const { data } = await api.invariants();
    const ok = data.balance_drift === 0;
    pill.className = `pill ${ok ? "pill-ok" : "pill-bad"}`;
    pill.textContent = ok ? "✓ баланс = сумма журнала" : `✗ расхождений: ${data.balance_drift}`;
  } catch {
    pill.className = "pill pill-muted";
    pill.textContent = "инвариант: нет данных";
  }
}

document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

async function main() {
  initTheme();
  initTopup();
  initGenerate();
  try {
    state.models = (await api.models()).data;
  } catch (error) {
    toast(errorText(error), "bad");
  }
  renderModels();
  renderProviders();
  renderScenarios();
  updatePreview();
  initUser();
  refreshInvariant();
  setInterval(refreshInvariant, 15000);
}

main();
