import { Link } from "react-router-dom";
import Page from "@/components/Page";
import { useAuth } from "@/lib/auth";
import { useRestoreTask, useTrash } from "@/lib/queries";

export default function Trash() {
  const { state } = useAuth();
  const trash = useTrash();
  const restore = useRestoreTask();

  if (state.status !== "authenticated" || !state.user.is_owner) {
    return <Page><p className="p-4">Bunga ruxsatingiz yo‘q.</p></Page>;
  }

  return <Page>
    <header className="px-4 pt-8 pb-5">
      <Link to="/board" className="text-sm text-muted underline">Doskaga qaytish</Link>
      <h1 className="mt-4 text-page font-semibold">O‘chirilganlar</h1>
      <p className="mt-2 text-sm text-muted">Vazifalar va ularning agent tarixi saqlanadi.</p>
    </header>
    {restore.isError && <p role="alert" className="px-4 text-sm text-late">Tiklab bo‘lmadi.</p>}
    <ul className="divide-y divide-hairline border-y border-hairline bg-card">
      {trash.data?.map((task) => <li key={task.id} className="flex items-center justify-between gap-3 px-4 py-4">
        <div>
          <p className="font-medium">{task.title}</p>
          <p className="mt-1 text-sm text-muted">{task.project.name} · #{task.id}</p>
        </div>
        <button
          type="button"
          disabled={restore.isPending}
          onClick={() => restore.mutate(task.id)}
          className="rounded-lg border border-hairline px-3 py-2 text-sm disabled:opacity-40"
        >Tiklash</button>
      </li>)}
    </ul>
    {trash.data?.length === 0 && <p className="px-4 py-8 text-muted">O‘chirilgan vazifa yo‘q.</p>}
  </Page>;
}
