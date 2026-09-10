import { useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "@/lib/auth";
import { useProjects, useTasks } from "@/lib/queries";
import { countTasks, isOverdue } from "@/lib/format";
import ProjectTag from "@/components/ProjectTag";
import NewProjectSheet from "@/components/NewProjectSheet";
import Empty from "@/components/Empty";

export default function Projects() {
  const { state } = useAuth();
  const isManager = state.status === "authenticated" && state.user.role === "manager";

  const [showArchived, setShowArchived] = useState(false);
  const [composing, setComposing] = useState(false);

  const projects = useProjects(showArchived);
  const { data } = useTasks({ open_only: true });
  const tasks = data?.items ?? [];

  return (
    <div className="pb-20 sm:pb-6">
      <header className="flex flex-wrap items-center gap-3 px-4 pt-8 pb-5">
        <h1 className="mr-auto text-page font-semibold">Loyihalar</h1>
        {isManager && (
          <button
            type="button"
            onClick={() => setComposing(true)}
            className="rounded-lg bg-ink px-3 py-2 text-sm font-semibold text-paper"
          >
            Loyiha qo‘shish
          </button>
        )}
      </header>

      <ul className="divide-y divide-hairline border-y border-hairline bg-card">
        {projects.data?.map((project) => {
          const mine = tasks.filter((task) => task.project.id === project.id);
          const late = mine.filter(isOverdue).length;
          return (
            <li key={project.id}>
              <Link
                to={`/projects/${project.id}`}
                className="flex items-center gap-3 px-4 py-4 hover:bg-ink/[0.03] dark:hover:bg-ink/[0.06]"
              >
                <span
                  aria-hidden
                  className="h-9 w-[3px] shrink-0 rounded-full"
                  style={{ backgroundColor: project.color }}
                />
                <span className="min-w-0 flex-1">
                  <span className="flex items-center gap-2">
                    <span className="truncate font-semibold">{project.name}</span>
                    <ProjectTag project={project} />
                    {project.is_archived && (
                      <span className="text-sm text-muted">arxivda</span>
                    )}
                  </span>
                  <span className="mt-0.5 block text-sm text-muted">
                    {countTasks(mine.length)} ochiq
                  </span>
                </span>
                {late > 0 && (
                  <span className="shrink-0 text-sm font-semibold text-late">
                    {late} kechikkan
                  </span>
                )}
              </Link>
            </li>
          );
        })}
      </ul>

      {projects.data?.length === 0 &&
        (isManager ? (
          <Empty
            title="Hali loyiha yo‘q."
            hint="Birinchisini qo‘shing — kaliti botda vazifa yozganda ishlatiladi."
          />
        ) : (
          <Empty
            title="Sizga loyiha biriktirilmagan."
            hint="Menejerdan loyihaga qo‘shishini so‘rang."
          />
        ))}

      <div className="px-4 pt-5">
        <button
          type="button"
          onClick={() => setShowArchived((value) => !value)}
          className="text-sm text-muted underline underline-offset-4"
        >
          {showArchived ? "Arxivdagilarni yashirish" : "Arxivdagilarni ko‘rsatish"}
        </button>
      </div>

      {composing && <NewProjectSheet onClose={() => setComposing(false)} />}
    </div>
  );
}
