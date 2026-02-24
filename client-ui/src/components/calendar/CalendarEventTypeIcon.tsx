import { ObjectIcons } from "../ObjectIcons";
import type { CalendarEventType } from "../../types";

type Props = {
  eventType: CalendarEventType;
  className?: string;
  status?: string;
};

export default function CalendarEventTypeIcon({
  eventType,
  className = "h-3.5 w-3.5",
  status,
}: Props) {
  if (eventType === "finding_created") {
    return <span className={className}>{ObjectIcons.findings}</span>;
  }
  if (eventType === "finding_mitigated") {
    return (
      <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
        <path fill="currentColor" d="M9.2 16.1 5.5 12.4l1.1-1.1 2.6 2.6 8.2-8.2 1.1 1.1-9.3 9.3Z" />
      </svg>
    );
  }
  if (eventType === "pipeline_scheduled") {
    return <span className={className}>{ObjectIcons.calendar}</span>;
  }
  if (eventType === "pipeline_started") {
    if (status && status.toUpperCase().includes("WARNING")) {
      return (
        <svg viewBox="0 0 24 24" className={className} aria-hidden="true">
          <path
            fill="currentColor"
            d="M12 4 3 18h18L12 4Zm0 3.07 7.43 10.93H4.57L12 7.07Zm-.5 4.88h1v3h-1Zm0-3h1v1.5h-1Z"
          />
        </svg>
      );
    }
    return <span className={className}>{ObjectIcons.pipelines}</span>;
  }
  return <span className={className}>{ObjectIcons.projects}</span>;
}
