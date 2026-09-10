import { useAuth } from "@/lib/auth";
import { useTasks } from "@/lib/queries";
import { countTasks, isOverdue, isToday } from "@/lib/format";
import TaskRow from "@/components/TaskRow";
import Empty from "@/components/Empty";
import type { Task } from "@/lib/types";
import Page from "@/components/Page";

function Section({ title, tasks, tz }: { title: string; tasks: Task[]; tz: string }) {
  if (!tasks.length) return null;
  return (
    <section className="mt-7">
      <h2 className="px-4 text-sm font-semibold">{title}</h2>
      <ul className="mt-1 divide-y divide-hairline border-y border-hairline bg-card pl-4">
        {tasks.map((task) => (
          <TaskRow key={task.id} task={task} tz={tz} />
        ))}
      </ul>
    </section>
  );
}

/**
 * The first screen answers one question: am I behind?
 *
 * So the headline is the count of late work, set large, rather than a row of
 * stat tiles. When nothing is late the same slot says so plainly — the state is
 * the headline either way.
 */
export default function MyDay() {
  const { state } = useAuth();
  const user = state.status === "authenticated" ? state.user : null;
  const tz = user?.tz ?? "Asia/Tashkent";

  const { data, isPending } = useTasks({ assignee_id: user?.id, open_only: true });
  const tasks = data?.items ?? [];

  const late = tasks.filter(isOverdue);
  const today = tasks.filter((task) => !isOverdue(task) && isToday(task, tz));
  const running = tasks.filter(
    (task) => task.status === "in_progress" && !isOverdue(task) && !isToday(task, tz),
  );
  const rest = tasks.filter(
    (task) => !late.includes(task) && !today.includes(task) && !running.includes(task),
  );

  return (
    <Page>
    <div className="pb-20 sm:pb-6">
      <header className="px-4 pt-8 pb-2">
        {isPending ? (
          <p className="text-muted">yuklanmoqda…</p>
        ) : late.length ? (
          <h1 className="text-display font-semibold text-late">
            {countTasks(late.length)} kechikdi
          </h1>
        ) : (
          <h1 className="text-display font-semibold">Hech narsa kechikmagan</h1>
        )}
        <p className="mt-2 text-muted">
          {tasks.length ? `${countTasks(tasks.length)} ochiq` : "Ochiq vazifa yo‘q"}
        </p>
      </header>

      <Section title="Kechikkan" tasks={late} tz={tz} />
      <Section title="Bugun" tasks={today} tz={tz} />
      <Section title="Bajarilmoqda" tasks={running} tz={tz} />
      <Section title="Keyingilar" tasks={rest} tz={tz} />

      {!isPending && !tasks.length && (
        <Empty
          title="Sizga biriktirilgan ochiq vazifa yo‘q."
          hint="Yangi vazifani botdan yozing yoki doskadan qo‘shing."
        />
      )}
    </div>
    </Page>
  );
}
