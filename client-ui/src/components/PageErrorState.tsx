import { isAccessDeniedError, isAuthExpiredError, toUserMessage } from "../lib/api";

type PageErrorStateProps = {
  error: unknown;
  fallbackTitle: string;
};

export default function PageErrorState({ error, fallbackTitle }: PageErrorStateProps) {
  const text = isAuthExpiredError(error)
    ? "Session expired, please login again."
    : isAccessDeniedError(error)
      ? "Access denied."
      : `${fallbackTitle}: ${toUserMessage(error)}`;
  return (
    <div className="rounded-2xl border border-danger-500/30 bg-night-700 p-6 text-sm text-danger-500">
      {text}
    </div>
  );
}
