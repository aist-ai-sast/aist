import { ApiError, isAccessDeniedError, isAuthExpiredError } from "./api";

// Shared retry policy for React Query's queries and mutations.
//
// 4xx (validation errors, not-found, ...) are deterministic — retrying just
// resends the same invalid request. Left unfiltered, a mutation kept
// retrying a 400 for the full backoff window (~3s across two retries),
// during which `isPending` — and any "busy"/disabled UI state derived from
// it, e.g. the Manage-access drawer's row selectors — stayed true, reading
// as a UI hang.
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (isAuthExpiredError(error) || isAccessDeniedError(error)) {
    return false;
  }
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
    return false;
  }
  return failureCount < 2;
}
