import axios from "axios";

// Backend address. Set NEXT_PUBLIC_API_URL in Vercel (or .env.local) to the
// deployed backend, e.g. https://logicforge-backend.onrender.com
export const API_URL = (
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"
).replace(/\/+$/, "");

export const API = axios.create({ baseURL: API_URL });

// ---- Access password (only used when the backend requires one) ----

const ACCESS_KEY = "logicforge.access";
export const UNAUTHORIZED_EVENT = "logicforge:unauthorized";

// Storage can be unavailable (private mode, blocked site data): never throw.
export function getAccessPassword(): string {
  try {
    return window.localStorage.getItem(ACCESS_KEY) || "";
  } catch {
    return "";
  }
}

export function setAccessPassword(password: string): void {
  try {
    window.localStorage.setItem(ACCESS_KEY, password);
  } catch {
    // Remembering the password is a convenience; ignore storage errors.
  }
}

export function clearAccessPassword(): void {
  try {
    window.localStorage.removeItem(ACCESS_KEY);
  } catch {
    // ignore
  }
}

API.interceptors.request.use((config) => {
  if (typeof window !== "undefined") {
    const password = getAccessPassword();
    if (password) config.headers.Authorization = `Bearer ${password}`;
  }
  return config;
});

// A 401 means the password is missing or wrong: let the page ask for it.
API.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401 && typeof window !== "undefined") {
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    }
    return Promise.reject(error);
  }
);
