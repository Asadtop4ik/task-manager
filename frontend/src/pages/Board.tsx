import { useState } from "react";
import { useAuth } from "@/lib/auth";
import { useProjects, useTasks, useTransition, useUsers } from "@/lib/queries";
import { BOARD_COLUMNS, STATUS_LABEL, TRANSITIONS, isOverdue } from "@/lib/format";
import TaskRow from "@/components/TaskRow";
import Empty from "@/components/Empty";
import type { Task, TaskStatus } from "@/lib/types";
import Page from "@/components/Page";

export default function Board() {
  const { state } = useAuth();
  const user = state.status === "authenticated" ? state.user : null;
  const tz = user?.tz ?? "Asia/Tashkent";

  const [projectId, setProjectId] = useState<number | "all">("all");
  const [assigneeId, setAssigneeId] = useState<number | "all">("all");
  const [onlyLate, setOnlyLate] = useState(false);
  const [column, setColumn] = useState<TaskStatus>("todo");
  const [dragging, setDragging] = useState<Task | null>(null);
  const [settled, setSettled] = useState<number | null>(null);

  const projects = useProjects();
  const users = useUsers();
  const transition = useTransition();

  const { data, isPending } = useTasks({
    open_only: true,
    project_id: projectId === "all" ? undefined : projectId,
    assignee_id: assigneeId === "all" ? undefined : assigneeId,
  });

  const tasks = (data?.items ?? []).filter((task) => !onlyLate || isOverdue(task));
  const inColumn = (status: TaskStatus) => tasks.filter((task) => task.status === status);

  function drop(status: TaskStatus) {
    const task = dragging;
    setDragging(null);
    // The same table the API enforces. Dropping somewhere impossible should do
    // nothing rather than flash the card there and snap it back.
    if (!task || task.status === status || !TRANSITIONS[task.status].includes(status)) return;
    transition.mutate({ id: task.id, status });
    // The card has already moved optimistically; the lift is what tells you
    // which one it was after your eye followed the cursor.
    setSettled(task.id);
    window.setTimeout(() => setSettled((current) => (current === task.id ? null : current)), 900);
  }

  return (
    <Page wide>
    <div className="pb-20 sm:pb-6">
      <header className="px-4 pt-8 pb-4">
        <h1 className="text-page font-semibold">Doska</h1>
      </header>

      <div className="flex flex-wrap gap-2 px-4 pb-4">
        <select
          value={projectId}
          onChange={(event) =>
            setProjectId(event.target.value === "all" ? "all" : Number(event.target.value))
          }
          aria-label="Loyiha"
          className="rounded-lg border border-hairline bg-card px-2.5 py-2 text-sm"
        >
          <option value="all">Barcha loyihalar</option>
          {projects.data?.map((project) => (
            <option key={project.id} value={project.id}>
              {project.name}
            </option>
          ))}
        </select>

        <select
          value={assigneeId}
          onChange={(event) =>
            setAssigneeId(event.target.value === "all" ? "all" : Number(event.target.value))
          }
          aria-label="Bajaruvchi"
          className="rounded-lg border border-hairline bg-card px-2.5 py-2 text-sm"
        >
          <option value="all">Hamma</option>
          {users.data?.map((person) => (
            <option key={person.id} value={person.id}>
              {person.full_name}
            </option>
          ))}
        </select>

        <label className="flex items-center gap-2 rounded-lg border border-hairline bg-card px-2.5 py-2 text-sm">
          <input
            type="checkbox"
            checked={onlyLate}
            onChange={(event) => setOnlyLate(event.target.checked)}
          />
          Faqat kechikkan
        </label>
      </div>

      {/* Phone: one column at a time. A four-column kanban at 390px is four
          unreadable columns, so the columns become a picker instead. */}
      <div className="sm:hidden">
        <div className="flex gap-1 overflow-x-auto px-4 pb-3">
          {BOARD_COLUMNS.map((status) => (
            <button
              key={status}
              type="button"
              onClick={() => setColumn(status)}
              className={[
                "shrink-0 rounded-full border px-3 py-1.5 text-sm",
                status === column
                  ? "border-ink bg-ink text-paper"
                  : "border-hairline bg-card text-muted",
              ].join(" ")}
            >
              {STATUS_LABEL[status]} {inColumn(status).length}
            </button>
          ))}
        </div>
        <ul className="divide-y divide-hairline border-y border-hairline bg-card pl-4">
          {inColumn(column).map((task) => (
            <TaskRow key={task.id} task={task} tz={tz} />
          ))}
        </ul>
        {!isPending && !inColumn(column).length && (
          <Empty title={`${STATUS_LABEL[column]} — bo‘sh.`} />
        )}
      </div>

      {/* Desktop: real columns, drag between them. */}
      <div className="hidden gap-3 px-4 sm:grid sm:grid-cols-4">
        {BOARD_COLUMNS.map((status) => {
          const rows = inColumn(status);
          const receiving =
            dragging && dragging.status !== status && TRANSITIONS[dragging.status].includes(status);
          return (
            <section
              key={status}
              onDragOver={(event) => {
                if (receiving) event.preventDefault();
              }}
              onDrop={() => drop(status)}
              className={[
                "min-h-40 rounded-xl border bg-card p-2",
                receiving ? "border-ink border-dashed" : "border-hairline",
              ].join(" ")}
            >
              <h2 className="flex items-baseline justify-between px-1 pb-2 text-sm font-semibold">
                {STATUS_LABEL[status]}
                <span className="text-muted">{rows.length}</span>
              </h2>
              <ul className="space-y-1">
                {rows.map((task) => (
                  <TaskRow
                    key={task.id}
                    task={task}
                    tz={tz}
                    draggable
                    onDragStart={() => setDragging(task)}
                    settling={settled === task.id}
                  />
                ))}
              </ul>
            </section>
          );
        })}
      </div>

      {!isPending && !tasks.length && (
        <Empty
          title="Bu filtrlarda vazifa yo‘q."
          hint="Filtrlarni kengaytiring yoki yangi vazifa qo‘shing."
        />
      )}

    </div>
    </Page>
  );
}
