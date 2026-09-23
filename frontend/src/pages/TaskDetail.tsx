import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useAuth } from "@/lib/auth";
import {
  useAgentRuns,
  useCancelAgentRun,
  useActivity,
  useAddComment,
  useAssign,
  useComments,
  useLogTime,
  useTask,
  useStartAgentRun,
  useTransition,
  useUpdateTask,
  useUsers,
} from "@/lib/queries";
import {
  PRIORITY_LABEL,
  STATUS_LABEL,
  formatDue,
  formatMinutes,
  isOverdue,
  lateness,
} from "@/lib/format";
import StatusMenu from "@/components/StatusMenu";
import ProjectTag from "@/components/ProjectTag";
import InlineText from "@/components/InlineText";
import type { Activity, TaskPriority, TaskStatus } from "@/lib/types";
import Page from "@/components/Page";

function describe(entry: Activity): string {
  const payload = entry.payload as Record<string, string | number | null>;
  switch (entry.kind) {
    case "created":
      return "yaratdi";
    case "status_changed":
      return `holatni ${STATUS_LABEL[payload.to as TaskStatus] ?? payload.to} ga o‘zgartirdi`;
    case "priority_changed":
      return `muhimligini ${PRIORITY_LABEL[payload.to as TaskPriority] ?? payload.to} qildi`;
    case "due_changed":
      return payload.to ? "muddatni o‘zgartirdi" : "muddatni olib tashladi";
    case "assigned":
      return payload.to ? "biriktirdi" : "biriktiruvni bekor qildi";
    case "commented":
      return "izoh qoldirdi";
    case "time_logged":
      return `${payload.minutes} daqiqa yozdi`;
    default:
      return entry.kind;
  }
}

export default function TaskDetail() {
  const { id } = useParams();
  const taskId = Number(id);
  const { state } = useAuth();
  const tz = state.status === "authenticated" ? state.user.tz : "Asia/Tashkent";

  const task = useTask(taskId);
  const comments = useComments(taskId);
  const activity = useActivity(taskId);
  const agentRuns = useAgentRuns(taskId);
  const startAgent = useStartAgentRun();
  const cancelAgent = useCancelAgentRun();
  const users = useUsers();

  const transition = useTransition();
  const assign = useAssign();
  const update = useUpdateTask();
  const logTime = useLogTime();
  const addComment = useAddComment();

  const [draft, setDraft] = useState("");
  const [minutes, setMinutes] = useState("");

  if (task.isPending) return <p className="p-4 text-muted">yuklanmoqda…</p>;
  if (task.isError || !task.data) {
    return (
      <div className="p-4">
        <p className="font-medium">Vazifa topilmadi.</p>
        <p className="mt-1 text-sm text-muted">
          O‘chirilgan bo‘lishi yoki sizga ko‘rinmasligi mumkin.
        </p>
        <Link to="/" className="mt-3 inline-block text-sm underline underline-offset-4">
          Bugunga qaytish
        </Link>
      </div>
    );
  }

  const item = task.data;
  const late = isOverdue(item);
  const canDelegate =
    state.status === "authenticated" &&
    (state.user.role === "manager" || item.assignee?.id === state.user.id);

  return (
    <Page>
    <div className="pb-20 sm:pb-6">
      <header className="border-b border-hairline px-4 pt-6 pb-5">
        {/* The bot's "Ochish" button deep-links straight here, so this screen is
            often someone's first. Give it a way back that is not the browser. */}
        <Link to="/" className="text-sm text-muted underline underline-offset-4">
          Orqaga
        </Link>

        <div className="mt-4 flex items-center gap-2 text-sm text-muted">
          <ProjectTag project={item.project} />
          <Link to={`/projects/${item.project.id}`} className="truncate hover:underline">
            {item.project.name}
          </Link>
          <span className="ml-auto">#{item.id}</span>
        </div>

        <h1 className="mt-2">
          <InlineText
            label="Sarlavha"
            value={item.title}
            className="text-page font-semibold"
            onSave={(title) => update.mutate({ id: item.id, title })}
          />
        </h1>

        {late && (
          <p className="mt-2 font-medium text-late">
            {lateness(item.due_at, tz)} — {formatDue(item.due_at, tz)}
          </p>
        )}

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <StatusMenu
            status={item.status}
            disabled={transition.isPending}
            onChange={(status) => transition.mutate({ id: item.id, status })}
          />

          <select
            value={item.assignee?.id ?? ""}
            onChange={(event) =>
              assign.mutate({
                id: item.id,
                assignee_id: event.target.value ? Number(event.target.value) : null,
              })
            }
            aria-label="Bajaruvchi"
            className="rounded-lg border border-hairline bg-card px-2.5 py-2 text-sm"
          >
            <option value="">biriktirilmagan</option>
            {users.data?.map((person) => (
              <option key={person.id} value={person.id}>
                {person.full_name}
              </option>
            ))}
          </select>

          <select
            value={item.priority}
            onChange={(event) =>
              update.mutate({ id: item.id, priority: event.target.value as TaskPriority })
            }
            aria-label="Muhimligi"
            className="rounded-lg border border-hairline bg-card px-2.5 py-2 text-sm"
          >
            {(Object.keys(PRIORITY_LABEL) as TaskPriority[]).map((value) => (
              <option key={value} value={value}>
                {PRIORITY_LABEL[value]}
              </option>
            ))}
          </select>
        </div>

        {!late && item.due_at && (
          <p className="mt-3 text-sm text-muted">Muddati {formatDue(item.due_at, tz)}</p>
        )}
        {item.spent_minutes > 0 && (
          <p className="mt-1 text-sm text-muted">
            Sarflangan vaqt {formatMinutes(item.spent_minutes)}
          </p>
        )}
      </header>

      {canDelegate && (
        <section className="border-b border-hairline px-4 py-5">
          <h2 className="text-sm font-semibold">Codex</h2>
          <p className="mt-1 text-sm text-muted">
            {item.project.repo_full_name
              ? `${item.project.repo_full_name} · ${item.project.default_branch ?? "branch tanlanmagan"}`
              : "Avval loyiha sozlamalarida repositoryni ko‘rsating."}
          </p>
          <button
            type="button"
            disabled={
              !item.project.repo_full_name ||
              !item.project.default_branch ||
              ["done", "cancelled"].includes(item.status) ||
              startAgent.isPending
            }
            onClick={() => startAgent.mutate(item.id)}
            className="mt-3 rounded-lg border border-hairline px-3 py-2 text-sm font-medium disabled:opacity-40"
          >
            {startAgent.isPending
              ? "Yuborilmoqda…"
              : agentRuns.data?.[0]?.status === "failed"
                ? "Qayta urinish"
                : "Codexga berish"}
          </button>
          {startAgent.isError && (
            <p className="mt-2 text-sm text-late">Codex ishga tushmadi. Repo va ulanishni tekshiring.</p>
          )}
          {agentRuns.data?.map((run) => (
            <div key={run.run_id} className="mt-3 text-sm">
              <span className="font-medium">{run.status}</span>
              {run.pr_url && (
                <a className="ml-3 underline" href={run.pr_url} target="_blank" rel="noreferrer">
                  PRni ochish
                </a>
              )}
              {run.github_run_url && (
                <a className="ml-3 underline" href={run.github_run_url} target="_blank" rel="noreferrer">
                  Jarayon
                </a>
              )}
              {run.error && <p className="text-late">{run.error}</p>}
              {["pending", "dispatching", "dispatched", "running", "pr_ready"].includes(run.status) && (
                <button
                  type="button"
                  disabled={cancelAgent.isPending}
                  onClick={() => cancelAgent.mutate({ taskId: item.id, runId: run.run_id })}
                  className="ml-3 text-late underline disabled:opacity-40"
                >
                  To‘xtatish
                </button>
              )}
              {run.input_tokens !== null && run.output_tokens !== null && (
                <p className="text-muted">
                  Codex: {run.input_tokens.toLocaleString()} kirish, {run.output_tokens.toLocaleString()} chiqish tokeni
                </p>
              )}
            </div>
          ))}
        </section>
      )}

      <section className="border-b border-hairline px-4 py-5">
        <InlineText
          label="Tavsif"
          value={item.description ?? ""}
          multiline
          placeholder="Tavsif qo‘shish"
          className="whitespace-pre-wrap"
          onSave={(description) => update.mutate({ id: item.id, description })}
        />
      </section>

      <section className="border-b border-hairline px-4 py-5">
        <h2 className="text-sm font-semibold">Vaqt yozish</h2>
        <form
          className="mt-2 flex gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            const value = Number(minutes);
            if (!Number.isFinite(value) || value <= 0) return;
            logTime.mutate({ id: item.id, minutes: value });
            setMinutes("");
          }}
        >
          <input
            inputMode="numeric"
            value={minutes}
            onChange={(event) => setMinutes(event.target.value)}
            placeholder="30"
            aria-label="Daqiqa"
            className="w-24 rounded-lg border border-hairline bg-paper px-3 py-2.5 text-sm"
          />
          <button
            type="submit"
            className="rounded-lg border border-hairline px-3 py-2.5 text-sm font-medium"
          >
            Qo‘shish
          </button>
        </form>
      </section>

      <section className="border-b border-hairline px-4 py-5">
        <h2 className="text-sm font-semibold">Izohlar</h2>

        <ul className="mt-3 space-y-4">
          {comments.data?.map((comment) => (
            <li key={comment.id}>
              <p className="text-sm text-muted">{comment.author?.full_name ?? "nomaʼlum"}</p>
              <p className="mt-0.5 whitespace-pre-wrap">{comment.body}</p>
            </li>
          ))}
          {comments.data?.length === 0 && <li className="text-sm text-muted">Hali izoh yo‘q.</li>}
        </ul>

        <form
          className="mt-4 flex flex-col gap-2 sm:flex-row"
          onSubmit={(event) => {
            event.preventDefault();
            if (!draft.trim()) return;
            addComment.mutate({ id: item.id, body: draft.trim() });
            setDraft("");
          }}
        >
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            rows={2}
            placeholder="Izoh yozish"
            className="flex-1 rounded-lg border border-hairline bg-paper px-3 py-2.5 text-base"
          />
          <button
            type="submit"
            disabled={!draft.trim() || addComment.isPending}
            className="self-end rounded-md bg-ink px-4 py-2 text-sm font-medium text-paper disabled:opacity-40"
          >
            Yuborish
          </button>
        </form>
      </section>

      <section className="px-4 py-5">
        <h2 className="text-sm font-semibold">Tarix</h2>
        <ul className="mt-3 space-y-2 text-sm">
          {activity.data?.map((entry) => (
            <li key={entry.id} className="flex gap-3">
              <span className="w-28 shrink-0 text-muted">{formatDue(entry.created_at, tz)}</span>
              <span>
                {entry.actor?.full_name ?? "tizim"} {describe(entry)}
              </span>
            </li>
          ))}
        </ul>
      </section>
    </div>
    </Page>
  );
}
