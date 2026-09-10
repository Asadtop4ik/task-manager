import { useAuth } from "@/lib/auth";
import { useProjects, useTasks } from "@/lib/queries";
import { countTasks, isOverdue } from "@/lib/format";
import Empty from "@/components/Empty";

export default function Projects() {
  const { state } = useAuth();
  const isManager = state.status === "authenticated" && state.user.role === "manager";

  const projects = useProjects();
  const { data } = useTasks({ open_only: true });
  const tasks = data?.items ?? [];

  return (
    <div className="pb-16 sm:pb-0">
      <header className="px-4 pt-8 pb-4">
        <h1 className="text-2xl font-semibold tracking-tight">Loyihalar</h1>
      </header>

      <ul className="divide-y divide-hairline border-y border-hairline bg-card">
        {projects.data?.map((project) => {
          const mine = tasks.filter((task) => task.project.id === project.id);
          const late = mine.filter(isOverdue).length;
          return (
            <li key={project.id} className="flex items-center gap-3 px-4 py-4">
              <span
                aria-hidden
                className="size-3 shrink-0 rounded-full"
                style={{ backgroundColor: project.color }}
              />
              <span className="min-w-0 flex-1">
                <span className="block font-medium">{project.name}</span>
                <span className="block text-sm text-muted">{countTasks(mine.length)} ochiq</span>
              </span>
              {late > 0 && <span className="text-sm font-medium text-late">{late} kechikkan</span>}
            </li>
          );
        })}
      </ul>

      {projects.data?.length === 0 && (
        <Empty
          title="Sizga loyiha biriktirilmagan."
          hint="Menejerdan loyihaga qo‘shishini so‘rang."
        />
      )}

      {isManager && (
        <p className="px-4 pt-6 text-sm text-muted">
          Loyiha qo‘shish va aʼzolarni boshqarish keyingi bosqichda.
        </p>
      )}
    </div>
  );
}
