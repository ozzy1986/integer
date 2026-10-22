// Thin client for the service API (same origin).

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail);
    this.status = status;
  }
}

function detailText(data) {
  if (!data || data.detail === undefined) return "";
  if (Array.isArray(data.detail)) return data.detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ");
  return String(data.detail);
}

async function request(method, path, body, headers = {}) {
  const response = await fetch(path, {
    method,
    headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...headers },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) throw new ApiError(response.status, detailText(data) || response.statusText);
  return { status: response.status, data };
}

const user = (id) => `/users/${encodeURIComponent(id)}`;

export const api = {
  models: () => request("GET", "/models"),
  balance: (userId) => request("GET", `${user(userId)}/balance?limit=50`),
  tasks: (userId) => request("GET", `${user(userId)}/tasks?limit=30`),
  task: (taskId) => request("GET", `/tasks/${encodeURIComponent(taskId)}`),
  generate: (body, idempotencyKey) => request("POST", "/generate", body, { "Idempotency-Key": idempotencyKey }),
  topup: (body) => request("POST", "/demo/topup", body),
  invariants: () => request("GET", "/health/invariants"),
};
