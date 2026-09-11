import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTasks } from "@/lib/queries";
import { STATUS_LABEL } from "@/lib/format";
import ProjectTag from "@/components/ProjectTag";

type Action = { id: string; label: string; hint?: string; run: () => void };

/**
 * Jump to anything without leaving the keyboard.
 *
 * Search runs against open tasks only. Finished work is what you stop thinking
 * about, and including it would push today's three matches below fifty closed
 * ones.
 */
export default function CommandPalette({
  onClose,
  onNewTask,
}: {
  onClose: () => void;
  onNewTask: () => void;
}) {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const listRef = useRef<HTMLUListElement>(null);

  const { data } = useTasks({ open_only: true, q: query.trim() || undefined, limit: 8 });

  const actions: Action[] = useMemo(() => {
    const pages: Action[] = [
      { id: "new", label: "Yangi vazifa", hint: "n", run: onNewTask },
      { id: "today", label: "Bugun", run: () => navigate("/") },
      { id: "board", label: "Doska", run: () => navigate("/board") },
      { id: "projects", label: "Loyihalar", run: () => navigate("/projects") },
    ];
    const term = query.trim().toLowerCase();
    return term ? pages.filter((page) => page.label.toLowerCase().includes(term)) : pages;
  }, [query, navigate, onNewTask]);

  const tasks = data?.items ?? [];
  const rows: Action[] = [
    ...tasks.map((task) => ({
      id: `task-${task.id}`,
      label: task.title,
      run: () => navigate(`/tasks/${task.id}`),
    })),
    ...actions,
  ];

  useEffect(() => setActive(0), [query]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") return onClose();
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setActive((index) => Math.min(index + 1, rows.length - 1));
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setActive((index) => Math.max(index - 1, 0));
      }
      if (event.key === "Enter") {
        event.preventDefault();
        const row = rows[active];
        if (row) {
          row.run();
          onClose();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [rows, active, onClose]);

  useEffect(() => {
    listRef.current?.children[active]?.scrollIntoView({ block: "nearest" });
  }, [active]);

  return (
    <div
      className="fixed inset-0 z-40 flex items-start justify-center bg-black/45 p-4 pt-[12vh]"
      onClick={onClose}
      role="presentation"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Qidiruv"
        onClick={(event) => event.stopPropagation()}
        className="w-full max-w-lg overflow-hidden rounded-xl border border-hairline bg-card"
      >
        <input
          autoFocus
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Vazifa qidirish yoki sahifaga o‘tish"
          aria-label="Qidiruv"
          className="w-full border-b border-hairline bg-transparent px-4 py-3.5 text-base outline-none"
        />

        <ul ref={listRef} className="max-h-80 overflow-y-auto py-1">
          {rows.map((row, index) => {
            const task = tasks.find((item) => `task-${item.id}` === row.id);
            return (
              <li key={row.id}>
                <button
                  type="button"
                  onMouseEnter={() => setActive(index)}
                  onClick={() => {
                    row.run();
                    onClose();
                  }}
                  className={[
                    "flex w-full items-center gap-2 px-4 py-2.5 text-left",
                    index === active ? "bg-ink/[0.06] dark:bg-ink/[0.10]" : "",
                  ].join(" ")}
                >
                  {task && <ProjectTag project={task.project} />}
                  <span className="min-w-0 flex-1 truncate">{row.label}</span>
                  <span className="shrink-0 text-sm text-muted">
                    {task ? STATUS_LABEL[task.status] : row.hint}
                  </span>
                </button>
              </li>
            );
          })}
          {rows.length === 0 && (
            <li className="px-4 py-6 text-center text-sm text-muted">
              Hech narsa topilmadi.
            </li>
          )}
        </ul>

        <p className="flex gap-5 border-t border-hairline px-4 py-2 text-sm text-muted">
          <span>↑↓ tanlash</span>
          <span>Enter ochish</span>
          <span>Esc yopish</span>
        </p>
      </div>
    </div>
  );
}
