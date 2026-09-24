import { useState } from "react";
import { Link } from "react-router-dom";
import { formatDue, isOverdue, lateness, STATUS_LABEL } from "@/lib/format";
import type { Task } from "@/lib/types";

type Props = {
  task: Task;
  tz: string;
  /** Off on a single-status column, where every row would repeat the same word. */
  showProject?: boolean;
  draggable?: boolean;
  onDragStart?: (event: React.DragEvent) => void;
  onDragEnd?: () => void;
  /** Marks this row as a board card, so the column can measure the gaps. */
  card?: boolean;
  /** Briefly lifts the row after it moves, so a drag shows you what changed. */
  settling?: boolean;
  onDelete?: () => void;
};

/**
 * One task, one row.
 *
 * The title owns the full width and the metadata sits under it, because this
 * row has to survive both a full-width list and a 190px board column. An
 * earlier version put the deadline in its own right-hand column and every board
 * title collapsed to "Ma…".
 *
 * The coloured rail is the project — three bots, three hues, already set in the
 * database — so a list can be scanned without reading any project names.
 * Lateness is the only other colour, and urgency is a dot rather than a second
 * red label, so red keeps one meaning per shape.
 */
export default function TaskRow({
  task,
  tz,
  showProject = true,
  draggable,
  onDragStart,
  onDragEnd,
  card,
  settling,
  onDelete,
}: Props) {
  const [menuOpen, setMenuOpen] = useState(false);
  const late = isOverdue(task);
  const due = formatDue(task.due_at, tz);
  const urgent = task.priority === "urgent";

  return (
    <li
      data-card={card ? "1" : undefined}
      className={["relative", late ? "bg-late-wash" : "", settling ? "settle" : ""].join(" ").trim()}
    >
      <Link
        to={`/tasks/${task.id}`}
        draggable={draggable}
        onDragStart={onDragStart}
        onDragEnd={onDragEnd}
        className={`flex gap-3 py-3 ${onDelete ? "pr-10" : "pr-3"} hover:bg-ink/[0.03] dark:hover:bg-ink/[0.06]`}
      >
        <span
          aria-hidden
          className="w-[3px] shrink-0 rounded-full"
          style={{ backgroundColor: task.project.color }}
        />

        <span className="min-w-0 flex-1">
          <span className="flex items-start gap-1.5">
            {urgent && (
              <span
                title="shoshilinch"
                className="mt-[7px] size-1.5 shrink-0 rounded-full bg-late"
              />
            )}
            <span className="line-clamp-2 font-medium">{task.title}</span>
          </span>

          {/* Space is the separator. A middle dot between every pair of facts is
              chrome that appears on every screen and means nothing. */}
          <span className="mt-1 flex flex-wrap items-baseline gap-x-3 text-sm">
            <span className="truncate text-muted">
              {showProject ? task.project.name : STATUS_LABEL[task.status]}
            </span>
            <span className="truncate text-muted">
              {task.assignee?.full_name ?? "biriktirilmagan"}
            </span>
            {late ? (
              <span className="ml-auto shrink-0 font-medium text-late">
                {lateness(task.due_at, tz)}
              </span>
            ) : (
              due && <span className="ml-auto shrink-0 text-muted">{due}</span>
            )}
          </span>
        </span>
      </Link>
      {onDelete && <div className="absolute right-1 top-2 z-10">
        <button
          type="button"
          aria-label={`${task.title} — amallar`}
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen((open) => !open)}
          className="rounded px-2 py-1 text-muted hover:bg-ink/10"
        >⋯</button>
        {menuOpen && <div role="menu" className="absolute right-0 top-8 min-w-28 rounded-lg border border-hairline bg-card p-1 shadow-lg">
          <button
            type="button"
            role="menuitem"
            className="w-full rounded px-3 py-2 text-left text-sm text-late hover:bg-ink/10"
            onClick={() => { setMenuOpen(false); onDelete(); }}
          >O‘chirish</button>
        </div>}
      </div>}
    </li>
  );
}
