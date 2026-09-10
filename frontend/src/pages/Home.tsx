import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Project, TaskList } from "@/lib/types";

const STATUS_LABEL: Record<string, string> = {
  backlog: "Backlog",
  todo: "To do",
  in_progress: "In progress",
  blocked: "Blocked",
  review: "Review",
  done: "Done",
  cancelled: "Cancelled",
};

export default function Home() {
  const { state, logout } = useAuth();
  const user = state.status === "authenticated" ? state.user : null;

  const { data: projects } = useQuery({
    queryKey: ["projects"],
    queryFn: async () => (await api.get<Project[]>("/projects")).data,
  });

  const { data: tasks } = useQuery({
    queryKey: ["tasks", "open"],
    queryFn: async () =>
      (await api.get<TaskList>("/tasks", { params: { open_only: true, limit: 20 } })).data,
  });

  return (
    <main className="mx-auto flex min-h-full max-w-3xl flex-col gap-8 px-4 py-10">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Task Manager</h1>
          <p className="mt-1 text-sm opacity-70">
            {user?.full_name} · {user?.role}
          </p>
        </div>
        <button
          type="button"
          onClick={() => void logout()}
          className="rounded-lg border border-black/15 px-3 py-1.5 text-sm dark:border-white/20"
        >
          Chiqish
        </button>
      </header>

      <section>
        <h2 className="text-sm font-medium uppercase tracking-wide opacity-60">Loyihalar</h2>
        <ul className="mt-3 flex flex-wrap gap-2">
          {projects?.map((project) => (
            <li
              key={project.id}
              className="flex items-center gap-2 rounded-full border border-black/10 px-3 py-1 text-sm dark:border-white/15"
            >
              <span
                className="size-2 rounded-full"
                style={{ backgroundColor: project.color }}
                aria-hidden
              />
              {project.name}
            </li>
          ))}
          {projects?.length === 0 && (
            <li className="text-sm opacity-70">Sizga hali loyiha biriktirilmagan.</li>
          )}
        </ul>
      </section>

      <section>
        <h2 className="text-sm font-medium uppercase tracking-wide opacity-60">
          Ochiq vazifalar {tasks ? `(${tasks.total})` : ""}
        </h2>
        <ul className="mt-3 divide-y divide-black/10 dark:divide-white/10">
          {tasks?.items.map((task) => (
            <li key={task.id} className="flex items-baseline gap-3 py-3">
              <span
                className="size-2 shrink-0 rounded-full"
                style={{ backgroundColor: task.project.color }}
                aria-hidden
              />
              <div className="min-w-0">
                <p className="truncate font-medium">{task.title}</p>
                <p className="mt-0.5 text-xs opacity-60">
                  {task.project.name} · {STATUS_LABEL[task.status] ?? task.status} ·{" "}
                  {task.assignee?.full_name ?? "biriktirilmagan"}
                </p>
              </div>
            </li>
          ))}
          {tasks?.items.length === 0 && (
            <li className="py-3 text-sm opacity-70">
              Ochiq vazifa yo‘q. Botdan birinchisini yarating.
            </li>
          )}
        </ul>
        <p className="mt-4 text-xs opacity-50">
          To‘liq doska 4-bosqichda. Hozircha bu faqat API ishlayotganini ko‘rsatadi.
        </p>
      </section>
    </main>
  );
}
