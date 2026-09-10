import { STATUS_LABEL, TRANSITIONS } from "@/lib/format";
import type { TaskStatus } from "@/lib/types";

type Props = {
  status: TaskStatus;
  onChange: (next: TaskStatus) => void;
  disabled?: boolean;
};

/**
 * The moves this task can actually make.
 *
 * Built from the same transition table the API enforces, so the menu never
 * offers something the server will refuse — a rejected option is worse than a
 * missing one, because the person has already decided before being told no.
 *
 * A native <select> on purpose: it is the one control that gives a phone its own
 * wheel picker and a keyboard user arrow keys, for free.
 */
export default function StatusMenu({ status, onChange, disabled }: Props) {
  return (
    <select
      value={status}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value as TaskStatus)}
      aria-label="Holatni o‘zgartirish"
      className="rounded-md border border-hairline bg-card px-2 py-1.5 text-sm disabled:opacity-50"
    >
      <option value={status}>{STATUS_LABEL[status]}</option>
      {TRANSITIONS[status].map((next) => (
        <option key={next} value={next}>
          → {STATUS_LABEL[next]}
        </option>
      ))}
    </select>
  );
}
