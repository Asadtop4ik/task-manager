import { Fragment, useRef, useState } from "react";
import { AxiosError } from "axios";
import { Link } from "react-router-dom";
import { useAuth } from "@/lib/auth";
import { useDeleteTask, useProjects, useReorder, useTasks, useTransition, useUsers } from "@/lib/queries";
import { BOARD_COLUMNS, STATUS_LABEL, TRANSITIONS, isOverdue } from "@/lib/format";
import TaskRow from "@/components/TaskRow";
import Empty from "@/components/Empty";
import Page from "@/components/Page";
import type { Task, TaskStatus } from "@/lib/types";

type Drop = { status: TaskStatus; index: number };

export default function Board() {
  const { state } = useAuth();
  const user = state.status === "authenticated" ? state.user : null;
  const tz = user?.tz ?? "Asia/Tashkent";

  const [projectId, setProjectId] = useState<number | "all">("all");
  const [assigneeId, setAssigneeId] = useState<number | "all">("all");
  const [onlyLate, setOnlyLate] = useState(false);
  const [column, setColumn] = useState<TaskStatus>("todo");
  const [dragging, setDragging] = useState<Task | null>(null);
  const [drop, setDrop] = useState<Drop | null>(null);
  const [settled, setSettled] = useState<number | null>(null);

  const columns = useRef<Partial<Record<TaskStatus, HTMLUListElement | null>>>({});

  const projects = useProjects();
  const users = useUsers();
  const transition = useTransition();
  const reorder = useReorder();
  const deleteTask = useDeleteTask();

  function deleteCard(task: Task) {
    if (!window.confirm(`“${task.title}” vazifasi o‘chirilsinmi? Uni keyin tiklash mumkin.`)) return;
    deleteTask.mutate(task.id);
  }

  const activeTasks = useTasks({
    status: BOARD_COLUMNS.filter((status) => status !== "done"),
    project_id: projectId === "all" ? undefined : projectId,
    assignee_id: assigneeId === "all" ? undefined : assigneeId,
  });
  const completedTasks = useTasks({
    status: ["done"],
    project_id: projectId === "all" ? undefined : projectId,
    assignee_id: assigneeId === "all" ? undefined : assigneeId,
  });

  // Completed history has its own page so it cannot crowd active work out of
  // the board's 100-task API limit.
  const tasks = [
    ...(activeTasks.data?.items ?? []),
    ...(completedTasks.data?.items ?? []),
  ].filter((task) => !onlyLate || isOverdue(task));
  const isPending = activeTasks.isPending || completedTasks.isPending;
  // Hand order, set by dragging. The API returns whatever order suits its own
  // query; the column is the thing people arrange.
  const inColumn = (status: TaskStatus) =>
    tasks.filter((task) => task.status === status).sort((a, b) => a.position - b.position);

  /** Which gap the cursor is nearest, by comparing it to each card's midpoint. */
  function gapAt(status: TaskStatus, clientY: number): number {
    const list = columns.current[status];
    if (!list) return 0;
    const cards = Array.from(list.children).filter(
      (node): node is HTMLElement => node instanceof HTMLElement && node.dataset.card === "1",
    );
    for (const [index, card] of cards.entries()) {
      const box = card.getBoundingClientRect();
      if (clientY < box.top + box.height / 2) return index;
    }
    return cards.length;
  }

  function canDrop(status: TaskStatus): boolean {
    if (!dragging) return false;
    return dragging.status === status || TRANSITIONS[dragging.status].includes(status);
  }

  function handleDrop(status: TaskStatus) {
    const task = dragging;
    const target = drop;
    setDragging(null);
    setDrop(null);
    if (!task || !target || !canDrop(status)) return;

    const siblings = inColumn(status).filter((row) => row.id !== task.id);
    const previous = siblings[target.index - 1];
    const following = siblings[target.index];

    if (task.status !== status) {
      transition.mutate({ id: task.id, status });
    }
    // Reorder even on a cross-column drop: the card should land where it was
    // dropped, not at whatever end of the new column its old position implies.
    reorder.mutate({
      id: task.id,
      previous_id: previous?.id ?? null,
      next_id: following?.id ?? null,
    });

    setSettled(task.id);
    window.setTimeout(() => setSettled((current) => (current === task.id ? null : current)), 900);
  }

  const marker = (status: TaskStatus, index: number) =>
    drop && drop.status === status && drop.index === index ? (
      <li aria-hidden className="mx-1 my-0.5 h-0.5 rounded-full bg-ink" />
    ) : null;

  return (
    <Page wide>
      <div className="pb-20 sm:pb-6">
        <header className="px-4 pt-8 pb-4">
          <div className="flex items-center justify-between gap-3">
            <h1 className="text-page font-semibold">Doska</h1>
            {user?.is_owner && <Link to="/trash" className="text-sm text-muted underline" title="O‘chirilgan vazifalarni bu yerda tiklash mumkin">O‘chirilganlar</Link>}
          </div>
          {deleteTask.isError && <p role="alert" className="mt-2 text-sm text-late">
            {deleteTask.error instanceof AxiosError && deleteTask.error.response?.status === 409
              ? "Avval faol Codex ishini to‘xtating, keyin vazifani o‘chiring."
              : "Vazifani o‘chirib bo‘lmadi."}
          </p>}
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
              <TaskRow key={task.id} task={task} tz={tz}
                onDelete={user?.is_owner ? () => deleteCard(task) : undefined} />
            ))}
          </ul>
          {!isPending && !inColumn(column).length && (
            <Empty
              title={column === "done" ? "Hozircha vazifa yo‘q" : `${STATUS_LABEL[column]} — bo‘sh.`}
            />
          )}
        </div>

        {/* Desktop: real columns. Drag between them to change status, drag within
            one to set the order you want to work in. */}
        <div className="hidden gap-3 px-4 sm:grid sm:grid-cols-4">
          {BOARD_COLUMNS.map((status) => {
            const rows = inColumn(status);
            const receiving = canDrop(status);
            return (
              <section
                key={status}
                onDragOver={(event) => {
                  if (!receiving) return;
                  event.preventDefault();
                  const index = gapAt(status, event.clientY);
                  setDrop((current) =>
                    current?.status === status && current.index === index
                      ? current
                      : { status, index },
                  );
                }}
                onDragLeave={(event) => {
                  if (!event.currentTarget.contains(event.relatedTarget as Node)) {
                    setDrop((current) => (current?.status === status ? null : current));
                  }
                }}
                onDrop={() => handleDrop(status)}
                className={[
                  "min-h-40 rounded-xl border bg-card p-2",
                  dragging && receiving ? "border-dashed border-ink" : "border-hairline",
                ].join(" ")}
              >
                <h2 className="flex items-baseline justify-between px-1 pb-2 text-sm font-semibold">
                  {STATUS_LABEL[status]}
                  <span className="text-muted">{rows.length}</span>
                </h2>
                {status === "done" && !isPending && rows.length === 0 && (
                  <p className="px-1 text-sm text-muted">Hozircha vazifa yo‘q</p>
                )}
                <ul ref={(node) => void (columns.current[status] = node)}>
                  {rows.map((task, index) => (
                    <Fragment key={task.id}>
                      {marker(status, index)}
                      <TaskRow
                        task={task}
                        tz={tz}
                        draggable={user?.role === "manager" || task.assignee?.id === user?.id}
                        card
                        onDelete={user?.is_owner ? () => deleteCard(task) : undefined}
                        onDragStart={() => setDragging(task)}
                        onDragEnd={() => {
                          setDragging(null);
                          setDrop(null);
                        }}
                        settling={settled === task.id}
                      />
                    </Fragment>
                  ))}
                  {marker(status, rows.length)}
                </ul>
              </section>
            );
          })}
        </div>

        {!isPending && !tasks.length && (
          <Empty
            title="Bu filtrlarda vazifa yo‘q."
            hint="Filtrlarni kengaytiring yoki ⌘K bosib yangi vazifa qo‘shing."
          />
        )}
      </div>
    </Page>
  );
}
