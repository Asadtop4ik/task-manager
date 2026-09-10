import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useAuth } from "@/lib/auth";
import {
  useAddMember,
  useMembers,
  useProjects,
  useRemoveMember,
  useTasks,
  useUpdateProject,
  useUsers,
} from "@/lib/queries";
import { countTasks, isOverdue } from "@/lib/format";
import ProjectTag, { PROJECT_COLOURS } from "@/components/ProjectTag";
import TaskRow from "@/components/TaskRow";
import { fieldClass, labelClass, primaryButton } from "@/components/Sheet";
import Page from "@/components/Page";

export default function ProjectDetail() {
  const { id } = useParams();
  const projectId = Number(id);
  const { state } = useAuth();
  const user = state.status === "authenticated" ? state.user : null;
  const isManager = user?.role === "manager";
  const tz = user?.tz ?? "Asia/Tashkent";

  const projects = useProjects(true);
  const project = projects.data?.find((row) => row.id === projectId);

  const members = useMembers(projectId);
  const users = useUsers();
  const update = useUpdateProject();
  const addMember = useAddMember();
  const removeMember = useRemoveMember();

  const { data } = useTasks({ project_id: projectId, open_only: true });
  const tasks = data?.items ?? [];
  const late = tasks.filter(isOverdue).length;

  const [name, setName] = useState<string | null>(null);
  const [adding, setAdding] = useState<number | "">("");

  if (projects.isPending) return <p className="p-4 text-muted">yuklanmoqda…</p>;
  if (!project) {
    return (
      <div className="p-4">
        <p className="font-medium">Loyiha topilmadi.</p>
        <Link to="/projects" className="mt-3 inline-block text-sm underline underline-offset-4">
          Loyihalarga qaytish
        </Link>
      </div>
    );
  }

  const memberIds = new Set(members.data?.map((row) => row.user.id));
  const addable = users.data?.filter((person) => !memberIds.has(person.id)) ?? [];

  return (
    <Page>
    <div className="pb-20 sm:pb-6">
      <header className="border-b border-hairline px-4 pt-6 pb-5">
        <Link to="/projects" className="text-sm text-muted underline underline-offset-4">
          Loyihalar
        </Link>

        <div className="mt-4 flex items-center gap-3">
          <span
            aria-hidden
            className="h-10 w-1 shrink-0 rounded-full"
            style={{ backgroundColor: project.color }}
          />
          <h1 className="min-w-0 flex-1 truncate text-display font-semibold">{project.name}</h1>
          <ProjectTag project={project} size="lg" />
        </div>

        <p className="mt-3 text-muted">
          {countTasks(tasks.length)} ochiq
          {late > 0 && <span className="ml-3 font-semibold text-late">{late} kechikkan</span>}
        </p>
      </header>

      <section className="border-b border-hairline px-4 py-5">
        <h2 className="text-sm font-semibold">Ochiq vazifalar</h2>
        <ul className="mt-2 -mx-4 divide-y divide-hairline border-y border-hairline bg-card pl-4">
          {tasks.slice(0, 8).map((task) => (
            <TaskRow key={task.id} task={task} tz={tz} showProject={false} />
          ))}
        </ul>
        {tasks.length === 0 && <p className="mt-2 text-sm text-muted">Ochiq vazifa yo‘q.</p>}
        {tasks.length > 8 && (
          <Link to="/board" className="mt-3 inline-block text-sm underline underline-offset-4">
            Doskada hammasini ko‘rish
          </Link>
        )}
      </section>

      <section className="border-b border-hairline px-4 py-5">
        <h2 className="text-sm font-semibold">Aʼzolar</h2>
        <ul className="mt-3 space-y-2">
          {members.data?.map((member) => (
            <li key={member.user.id} className="flex items-center gap-3">
              <span className="min-w-0 flex-1 truncate">
                {member.user.full_name}
                <span className="ml-2 text-sm text-muted">{member.role_in_project}</span>
              </span>
              {isManager && member.user.id !== user?.id && (
                <button
                  type="button"
                  onClick={() =>
                    removeMember.mutate({ projectId, userId: member.user.id })
                  }
                  className="text-sm text-muted underline underline-offset-4"
                >
                  Chiqarish
                </button>
              )}
            </li>
          ))}
          {members.data?.length === 0 && (
            <li className="text-sm text-muted">Hali aʼzo yo‘q.</li>
          )}
        </ul>

        {isManager && addable.length > 0 && (
          <div className="mt-4 flex gap-2">
            <select
              value={adding}
              onChange={(event) => setAdding(Number(event.target.value))}
              aria-label="Aʼzo qo‘shish"
              className="flex-1 rounded-lg border border-hairline bg-paper px-3 py-2.5 text-sm"
            >
              <option value="">Kimni qo‘shamiz?</option>
              {addable.map((person) => (
                <option key={person.id} value={person.id}>
                  {person.full_name}
                </option>
              ))}
            </select>
            <button
              type="button"
              disabled={!adding}
              onClick={() => {
                if (!adding) return;
                addMember.mutate({ projectId, userId: adding });
                setAdding("");
              }}
              className={primaryButton}
            >
              Qo‘shish
            </button>
          </div>
        )}
      </section>

      {isManager && (
        <section className="px-4 py-5">
          <h2 className="text-sm font-semibold">Sozlamalar</h2>

          <label className={`mt-3 ${labelClass}`}>
            Nomi
            <input
              value={name ?? project.name}
              onChange={(event) => setName(event.target.value)}
              onBlur={() => {
                const next = (name ?? "").trim();
                if (next && next !== project.name) update.mutate({ id: projectId, name: next });
                setName(null);
              }}
              className={fieldClass}
            />
          </label>

          <fieldset className="mt-4">
            <legend className={labelClass}>Rangi</legend>
            <div className="mt-2 flex flex-wrap gap-2">
              {PROJECT_COLOURS.map((option) => (
                <button
                  key={option}
                  type="button"
                  aria-label={`Rang ${option}`}
                  aria-pressed={option === project.color}
                  onClick={() => update.mutate({ id: projectId, color: option })}
                  className={[
                    "size-8 rounded-lg border-2",
                    option === project.color ? "border-ink" : "border-transparent",
                  ].join(" ")}
                  style={{ backgroundColor: option }}
                />
              ))}
            </div>
          </fieldset>

          <div className="mt-5">
            <button
              type="button"
              onClick={() => update.mutate({ id: projectId, is_archived: !project.is_archived })}
              className="rounded-lg border border-hairline px-3 py-2.5 text-sm font-medium"
            >
              {project.is_archived ? "Arxivdan chiqarish" : "Arxivga solish"}
            </button>
            <p className="mt-2 text-sm text-muted">
              Arxivdagi loyiha ro‘yxatlarda ko‘rinmaydi. Vazifalari saqlanib qoladi.
            </p>
          </div>
        </section>
      )}
    </div>
    </Page>
  );
}
