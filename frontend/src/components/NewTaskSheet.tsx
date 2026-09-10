import { useState } from "react";
import { useCreateTask, useProjects, useUsers } from "@/lib/queries";
import { PRIORITY_LABEL } from "@/lib/format";
import Sheet, { fieldClass, labelClass, primaryButton, quietButton } from "@/components/Sheet";
import type { TaskPriority } from "@/lib/types";

/**
 * Compose a task.
 *
 * A sheet rather than a page: creating a task is a two-line thought, and losing
 * the board behind it makes you forget which column you were looking at.
 */
export default function NewTaskSheet({ onClose }: { onClose: () => void }) {
  const projects = useProjects();
  const users = useUsers();
  const create = useCreateTask();

  const [title, setTitle] = useState("");
  const [projectId, setProjectId] = useState<number | "">("");
  const [assigneeId, setAssigneeId] = useState<number | "">("");
  const [priority, setPriority] = useState<TaskPriority>("normal");
  const [due, setDue] = useState("");

  const chosenProject = projectId || projects.data?.[0]?.id;
  const canSubmit = Boolean(title.trim() && chosenProject) && !create.isPending;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!canSubmit || !chosenProject) return;
    await create.mutateAsync({
      project_id: chosenProject,
      title: title.trim(),
      priority,
      assignee_id: assigneeId || null,
      // datetime-local has no zone; the browser reads it as local time, which is
      // the time the person meant, and toISOString sends UTC.
      due_at: due ? new Date(due).toISOString() : null,
    });
    onClose();
  }

  return (
    <Sheet title="Yangi vazifa" onClose={onClose}>
      <form onSubmit={submit}>
        <label className={`mt-4 ${labelClass}`}>
          Nima qilish kerak
          <input
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="Mini app url ni tuzatish"
            className={fieldClass}
          />
        </label>

        <div className="mt-3 grid grid-cols-2 gap-3">
          <label className={labelClass}>
            Loyiha
            <select
              value={chosenProject ?? ""}
              onChange={(event) => setProjectId(Number(event.target.value))}
              className="mt-1 w-full rounded-lg border border-hairline bg-paper px-2.5 py-2.5 text-sm"
            >
              {projects.data?.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </label>

          <label className={labelClass}>
            Muhimligi
            <select
              value={priority}
              onChange={(event) => setPriority(event.target.value as TaskPriority)}
              className="mt-1 w-full rounded-lg border border-hairline bg-paper px-2.5 py-2.5 text-sm"
            >
              {(Object.keys(PRIORITY_LABEL) as TaskPriority[]).map((value) => (
                <option key={value} value={value}>
                  {PRIORITY_LABEL[value]}
                </option>
              ))}
            </select>
          </label>

          <label className={labelClass}>
            Kimga
            <select
              value={assigneeId}
              onChange={(event) =>
                setAssigneeId(event.target.value ? Number(event.target.value) : "")
              }
              className="mt-1 w-full rounded-lg border border-hairline bg-paper px-2.5 py-2.5 text-sm"
            >
              <option value="">biriktirmasdan</option>
              {users.data?.map((person) => (
                <option key={person.id} value={person.id}>
                  {person.full_name}
                </option>
              ))}
            </select>
          </label>

          <label className={labelClass}>
            Muddati
            <input
              type="datetime-local"
              value={due}
              onChange={(event) => setDue(event.target.value)}
              className="mt-1 w-full rounded-lg border border-hairline bg-paper px-2.5 py-2.5 text-sm"
            />
          </label>
        </div>

        {create.isError && (
          <p className="mt-3 text-sm text-late">
            Saqlanmadi. Internetni tekshiring va qaytadan urinib ko‘ring.
          </p>
        )}

        <div className="mt-5 flex justify-end gap-2">
          <button type="button" onClick={onClose} className={quietButton}>
            Bekor qilish
          </button>
          <button type="submit" disabled={!canSubmit} className={primaryButton}>
            {create.isPending ? "Saqlanmoqda…" : "Vazifa qo‘shish"}
          </button>
        </div>
      </form>
    </Sheet>
  );
}
