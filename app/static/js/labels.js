// Russian labels for API values, provider behaviours and one-click scenarios.

export const TASK_STATUS = {
  in_progress: ["в работе", "pill-run"],
  succeeded: ["готово", "pill-ok"],
  failed: ["провал · деньги возвращены", "pill-bad"],
};

export const ATTEMPT_STATUS = {
  pending: ["отправляется", "pill-run"],
  sent: ["ждём вебхук", "pill-run"],
  succeeded: ["успех", "pill-ok"],
  failed: ["отказ", "pill-bad"],
  superseded: ["больше не нужна", "pill-muted"],
};

export const REASONS = {
  webhook: "отказ пришёл вебхуком",
  watchdog_timeout: "сторож: провайдер молчал дольше PROVIDER_TIMEOUT",
  send_timeout: "таймаут при отправке POST /tasks",
  connect_error: "нет соединения с провайдером",
  bad_response: "некорректный ответ провайдера",
  unknown_provider: "провайдер не настроен",
};

export function reasonText(reason) {
  if (!reason) return "";
  if (REASONS[reason]) return REASONS[reason];
  const http = /^http_(\d{3})$/.exec(reason);
  return http ? `HTTP ${http[1]} при отправке` : reason;
}

export const LEDGER_KIND = { payment: "Пополнение", charge: "Списание", refund: "Возврат" };

export function eventText(event) {
  const d = event.detail || {};
  switch (event.kind) {
    case "created": return `Задача создана: ${d.model}, референсов ${d.refs}`;
    case "charged": return `Списано ${d.amount}`;
    case "attempt_created": return `Новая попытка → ${d.provider} (нога ${d.priority})`;
    case "attempt_sent": return `${d.provider} принял задачу (${d.external_id})`;
    case "attempt_failed": return `Отказ ${d.error}: ${reasonText(d.reason)}`;
    case "attempt_superseded": return "Попытка больше не нужна: результат уже получен";
    case "task_succeeded": return d.late ? "Готово — принят поздний результат (правило 6)" : "Готово";
    case "task_failed": return `Задача провалена (${d.error})`;
    case "refunded": return `Возвращено ${d.amount}`;
    case "duplicate_ignored": return `Дубль вебхука (${d.status}) — проигнорирован`;
    case "late_result_ignored": return "Поздний результат проигнорирован: деньги уже возвращены (правило 6)";
    default: return event.kind;
  }
}

// What a provider will do with this task. Directives travel inside the prompt; the service never reads them.
export const BEHAVIOURS = [
  { id: "ok", label: "Успех", make: () => [] },
  { id: "technical", label: "Отказ: technical", make: (p) => [`@${p}:fail=technical`] },
  { id: "moderation", label: "Отказ: moderation", make: (p) => [`@${p}:fail=moderation`] },
  { id: "payload", label: "Отказ: payload", make: (p) => [`@${p}:fail=payload`] },
  { id: "sync503", label: "503 на POST /tasks", make: (p) => [`@${p}:sync=503`], instant: true },
  { id: "synctimeout", label: "Таймаут на POST /tasks", make: (p) => [`@${p}:sync=timeout`] },
  { id: "dup", label: "Успех, вебхук ×10 одновременно", make: (p) => [`@${p}:dup=10`] },
  { id: "faildup", label: "Отказ, вебхук ×10 одновременно", make: (p) => [`@${p}:fail`, `@${p}:dup=10`] },
  { id: "slow", label: "Медленно: ответ через 40 с", make: (p) => [`@${p}:delay=40`], timed: true },
  { id: "drop", label: "Молчит → сработает сторож", make: (p) => [`@${p}:drop`], timed: true },
  { id: "late", label: "Ответит после сторожа (75 с)", make: (p) => [`@${p}:late`], timed: true },
  { id: "verylate", label: "Ответит после возврата (140 с)", make: (p) => [`@${p}:late=140`], timed: true },
];

export const SCENARIOS = [
  { label: "Всё хорошо", model: "image-fast", alpha: "ok", beta: "ok" },
  { label: "Отказ первой ноги", model: "image-fast", alpha: "technical", beta: "ok" },
  { label: "Модерация: alpha пропускается", model: "video-long", alpha: "ok", beta: "moderation" },
  { label: "Payload: сразу провал", model: "image-fast", alpha: "payload", beta: "ok" },
  { label: "503 при отправке", model: "image-fast", alpha: "sync503", beta: "ok" },
  { label: "10 отказов на последней ноге", model: "image-fast", alpha: "technical", beta: "faildup" },
  { label: "Провайдер молчит (~60 с)", model: "image-fast", alpha: "drop", beta: "ok" },
  { label: "Поздний ответ, задача в работе", model: "image-fast", alpha: "late", beta: "slow" },
  { label: "Поздний ответ после возврата", model: "image-fast", alpha: "verylate", beta: "drop" },
];

export function errorText(error) {
  switch (error.status) {
    case 400: return `400 · ${error.message}`;
    case 402: return "402 · Не хватает токенов — пополните баланс";
    case 404: return `404 · ${error.message}`;
    case 409: return "409 · Этот Idempotency-Key уже использован для другого запроса";
    case 413: return "413 · Слишком большой запрос";
    case 422: return `422 · Неверные данные: ${error.message}`;
    case 429: return "429 · У пользователя уже 3 активные задачи — дождитесь завершения";
    default: return error.status ? `${error.status} · ${error.message}` : "Сервис недоступен";
  }
}
