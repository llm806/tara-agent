import type {
  AuthResponse,
  AuthUser,
  PageInfo,
  QuestionSuggestionListResponse,
  SessionDetail,
  SessionDeleteResponse,
  SessionListResponse,
  SessionSummary,
  TraceDetail,
  TraceListResponse,
  TraceSummary,
} from "@/lib/types";

const configuredApiBaseUrl = process.env.NEXT_PUBLIC_API_BASE_URL?.trim();

export const apiBaseUrl = configuredApiBaseUrl || defaultApiBaseUrl();

function defaultApiBaseUrl(): string {
  if (typeof window === "undefined") {
    return "http://localhost:8000";
  }
  return window.location.origin;
}

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "ApiError";
  }
}

export function getCurrentUser(signal?: AbortSignal): Promise<AuthUser> {
  return requestJson("/api/v1/auth/me", signal);
}

export function getQuestionSuggestions(
  excludeIds: string[] = [],
  signal?: AbortSignal,
): Promise<QuestionSuggestionListResponse> {
  const query = new URLSearchParams({ limit: "4" });
  for (const id of excludeIds) {
    query.append("exclude_id", id);
  }
  return requestJson(`/api/v1/question-suggestions?${query.toString()}`, signal);
}

export function login(
  email: string,
  password: string,
  signal?: AbortSignal,
): Promise<AuthResponse> {
  return sendJson("/api/v1/auth/login", { email, password }, signal);
}

export function loginAsGuest(signal?: AbortSignal): Promise<AuthResponse> {
  return sendJson("/api/v1/auth/guest", {}, signal);
}

export function register(
  email: string,
  displayName: string,
  password: string,
  signal?: AbortSignal,
): Promise<AuthResponse> {
  return sendJson(
    "/api/v1/auth/register",
    { email, display_name: displayName, password },
    signal,
  );
}

export async function logout(): Promise<void> {
  const response = await fetch(`${apiBaseUrl}/api/v1/auth/logout`, {
    method: "POST",
    credentials: "include",
  });
  if (!response.ok) {
    throw await apiError(response);
  }
}

export function listSessions(signal?: AbortSignal): Promise<SessionListResponse> {
  return listAllPages<SessionSummary>(
    (limit, offset) => `/api/v1/sessions?limit=${limit}&offset=${offset}`,
    signal,
  );
}

export function getSession(sessionId: string, signal?: AbortSignal): Promise<SessionDetail> {
  return requestJson(`/api/v1/sessions/${encodeURIComponent(sessionId)}`, signal);
}

export async function deleteSession(sessionId: string): Promise<void> {
  const response = await fetch(
    `${apiBaseUrl}/api/v1/sessions/${encodeURIComponent(sessionId)}`,
    { method: "DELETE", credentials: "include" },
  );
  if (!response.ok) {
    throw await apiError(response);
  }
}

export async function deleteSessions(sessionIds: string[]): Promise<SessionDeleteResponse> {
  const response = await fetch(`${apiBaseUrl}/api/v1/sessions`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    credentials: "include",
    body: JSON.stringify({ session_ids: sessionIds }),
  });
  if (!response.ok) {
    throw await apiError(response);
  }
  return response.json() as Promise<SessionDeleteResponse>;
}

export async function updateSession(
  sessionId: string,
  changes: { title?: string; pinned?: boolean },
): Promise<SessionSummary> {
  const response = await fetch(
    `${apiBaseUrl}/api/v1/sessions/${encodeURIComponent(sessionId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      credentials: "include",
      body: JSON.stringify(changes),
    },
  );
  if (!response.ok) {
    throw await apiError(response);
  }
  return response.json() as Promise<SessionSummary>;
}

export function getTrace(traceId: string, signal?: AbortSignal): Promise<TraceDetail> {
  return requestJson(`/api/v1/traces/${encodeURIComponent(traceId)}`, signal);
}

export function listSessionTraces(
  sessionId: string,
  signal?: AbortSignal,
): Promise<TraceListResponse> {
  return listAllPages<TraceSummary>(
    (limit, offset) => {
      const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
      return `/api/v1/sessions/${encodeURIComponent(sessionId)}/traces?${query.toString()}`;
    },
    signal,
  );
}

export function getSessionTrace(
  sessionId: string,
  traceId: string,
  signal?: AbortSignal,
): Promise<TraceDetail> {
  return requestJson(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/traces/${encodeURIComponent(traceId)}`,
    signal,
  );
}

async function requestJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${apiBaseUrl}${path}`, {
    headers: { Accept: "application/json" },
    credentials: "include",
    signal,
  });
  if (!response.ok) {
    throw await apiError(response);
  }
  return response.json() as Promise<T>;
}

const pageSize = 100;

async function listAllPages<Item>(
  pathForPage: (limit: number, offset: number) => string,
  signal?: AbortSignal,
): Promise<{ items: Item[]; page: PageInfo }> {
  const items: Item[] = [];
  let total = 0;

  do {
    const response = await requestJson<{ items: Item[]; page: PageInfo }>(
      pathForPage(pageSize, items.length),
      signal,
    );
    total = response.page.total;
    items.push(...response.items);
    if (response.items.length === 0) break;
  } while (items.length < total);

  return { items, page: { limit: pageSize, offset: 0, total } };
}

async function sendJson<T>(
  path: string,
  body: Record<string, string>,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(`${apiBaseUrl}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    credentials: "include",
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    throw await apiError(response);
  }
  return response.json() as Promise<T>;
}

async function apiError(response: Response): Promise<ApiError> {
  if (response.status === 401 && typeof window !== "undefined") {
    window.dispatchEvent(new Event("tara-auth-expired"));
  }
  const payload = await response.json().catch(() => null) as { detail?: string } | null;
  return new ApiError(payload?.detail ?? `请求失败（HTTP ${response.status}）`, response.status);
}
