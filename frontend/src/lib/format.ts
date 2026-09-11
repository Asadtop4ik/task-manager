import type { Task, TaskPriority, TaskStatus } from "@/lib/types";

export const STATUS_LABEL: Record<TaskStatus, string> = {
  backlog: "Navbatda",
  todo: "Bajarilishi kerak",
  in_progress: "Bajarilmoqda",
  blocked: "To‘xtatilgan",
  review: "Tekshiruvda",
  done: "Bajarildi",
  cancelled: "Bekor qilindi",
};

export const PRIORITY_LABEL: Record<TaskPriority, string> = {
  low: "past",
  normal: "oddiy",
  high: "muhim",
  urgent: "shoshilinch",
};

export const OPEN_STATUSES: TaskStatus[] = [
  "backlog",
  "todo",
  "in_progress",
  "blocked",
  "review",
];

// The board's columns. Backlog is deliberately absent: it is a holding pen, not
// a stage of work, and a fifth column of things nobody is doing makes the four
// that matter narrower.
export const BOARD_COLUMNS: TaskStatus[] = ["todo", "in_progress", "blocked", "review"];

// Mirrors the API's transition table (app/db/enums.py). The UI must not offer a
// move the server will refuse.
//
// Any open status reaches any other: a board is dragged in both directions, and
// putting a card back where it came from is an ordinary correction. The guards
// that remain are on the terminal states — only started work can be finished,
// and done or cancelled reopens to todo and nowhere else.
const OPEN: TaskStatus[] = ["backlog", "todo", "in_progress", "blocked", "review"];
const CAN_FINISH: TaskStatus[] = ["in_progress", "review"];

export const TRANSITIONS: Record<TaskStatus, TaskStatus[]> = {
  ...(Object.fromEntries(
    OPEN.map((status) => [
      status,
      [
        ...OPEN.filter((other) => other !== status),
        ...(CAN_FINISH.includes(status) ? (["done"] as TaskStatus[]) : []),
        "cancelled" as TaskStatus,
      ],
    ]),
  ) as Record<TaskStatus, TaskStatus[]>),
  done: ["todo"],
  cancelled: ["todo"],
};

const MONTHS = [
  "yanvar", "fevral", "mart", "aprel", "may", "iyun",
  "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr",
];

function partsIn(value: Date, tz: string) {
  // Everything is stored UTC and read in the viewer's own zone; a deadline shown
  // in UTC to someone in Tashkent is five hours wrong.
  const formatter = new Intl.DateTimeFormat("en-GB", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  const fields: Record<string, string> = {};
  for (const part of formatter.formatToParts(value)) fields[part.type] = part.value;
  return {
    year: Number(fields.year),
    month: Number(fields.month),
    day: Number(fields.day),
    hour: fields.hour ?? "00",
    minute: fields.minute ?? "00",
  };
}

function dayIndex(value: Date, tz: string): number {
  const { year, month, day } = partsIn(value, tz);
  return Date.UTC(year, month - 1, day) / 86_400_000;
}

/** A deadline in words: "bugun 18:00", "ertaga 09:00", "12-sentabr 18:00". */
export function formatDue(iso: string | null, tz: string): string | null {
  if (!iso) return null;
  const moment = new Date(iso);
  const { month, day, hour, minute } = partsIn(moment, tz);
  const clock = `${hour}:${minute}`;
  const offset = dayIndex(moment, tz) - dayIndex(new Date(), tz);

  if (offset === 0) return `bugun ${clock}`;
  if (offset === 1) return `ertaga ${clock}`;
  if (offset === -1) return `kecha ${clock}`;
  return `${day}-${MONTHS[month - 1]} ${clock}`;
}

/** How late, in whole days, phrased for a person: "2 kun kechikdi". */
export function lateness(iso: string | null, tz: string): string | null {
  if (!iso) return null;
  const days = dayIndex(new Date(), tz) - dayIndex(new Date(iso), tz);
  if (days <= 0) return "muddati o‘tdi";
  if (days === 1) return "1 kun kechikdi";
  return `${days} kun kechikdi`;
}

export function isOverdue(task: Task): boolean {
  if (!task.due_at || !OPEN_STATUSES.includes(task.status)) return false;
  return new Date(task.due_at).getTime() < Date.now();
}

export function isToday(task: Task, tz: string): boolean {
  if (!task.due_at) return false;
  return dayIndex(new Date(task.due_at), tz) === dayIndex(new Date(), tz);
}

export function formatMinutes(total: number): string {
  const hours = Math.floor(total / 60);
  const minutes = total % 60;
  if (!hours) return `${minutes} daq`;
  return minutes ? `${hours} soat ${minutes} daq` : `${hours} soat`;
}

/** "3 ta vazifa", with Uzbek's invariant noun after a numeral. */
export function countTasks(n: number): string {
  return `${n} ta vazifa`;
}
