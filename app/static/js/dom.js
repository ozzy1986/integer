// Tiny DOM helpers. Text always goes through text nodes, never parsed as HTML, so API data cannot inject markup.

export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function safeHttpUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}

export function toast(text, kind = "info", ms = 5200) {
  const box = document.getElementById("toasts");
  const el = h("div", { class: `toast ${kind}`, role: kind === "bad" ? "alert" : "status" }, text);
  box.append(el);
  setTimeout(() => el.remove(), ms);
}

const timeFormat = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
export const fmtTime = (iso) => (iso ? timeFormat.format(new Date(iso)) : "");

export function fmtDuration(fromIso, toIso) {
  if (!fromIso || !toIso) return "";
  const seconds = (new Date(toIso) - new Date(fromIso)) / 1000;
  return seconds < 10 ? `${seconds.toFixed(1)} с` : `${Math.round(seconds)} с`;
}

export function plural(n, one, few, many) {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

export const tokens = (n) => `${n} ${plural(Math.abs(n), "токен", "токена", "токенов")}`;

// Browser storage is a convenience only: private windows may refuse it.
export const storage = {
  get(key) {
    try { return localStorage.getItem(key); } catch { return null; }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); } catch { /* ignore */ }
  },
};

export function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const hex = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
