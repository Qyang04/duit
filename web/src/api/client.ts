// Typed API client. `schema.d.ts` is generated from the API's OpenAPI document
// (`npm run gen:api`), so a renamed field or endpoint is a compile error here, not a runtime bug.
import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

export type Transaction = components["schemas"]["TransactionOut"];
export type TransactionPage = components["schemas"]["TransactionPage"];
export type Category = components["schemas"]["CategoryOut"];
export type Account = components["schemas"]["AccountOut"];
export type User = components["schemas"]["UserOut"];

// Same origin: the session cookie (HttpOnly, SameSite=Strict) rides along automatically.
export const api = createClient<paths>({ baseUrl: "", credentials: "same-origin" });

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

function detailOf(error: unknown): string {
  if (error && typeof error === "object" && "detail" in error) {
    const detail = (error as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail) && detail[0]?.msg) return String(detail[0].msg);
  }
  return "Something went wrong";
}

/** The data of a successful response, or an ApiError carrying the API's message. */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (!result.response.ok) throw new ApiError(result.response.status, detailOf(result.error));
  return result.data as T;
}
