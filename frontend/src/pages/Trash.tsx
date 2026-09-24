import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import Page from "@/components/Page";
import { useAuth } from "@/lib/auth";
import { useRestoreTask, useTrash } from "@/lib/queries";

export default function Trash() {
  const { state } = useAuth();
  const [offset, setOffset] = useState(0);
  const trash = useTrash(offset);
  const restore = useRestoreTask();

  useEffect(() => {
    if (trash.data && offset > 0 && offset >= trash.data.total) {
      setOffset(Math.max(0, offset - 50));
    }
  }, [offset, trash.data]);

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
      {trash.data?.items.map((task) => <li key={task.id} className="flex items-center justify-between gap-3 px-4 py-4">
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
    {trash.data?.total === 0 && <p className="px-4 py-8 text-muted">O‘chirilgan vazifa yo‘q.</p>}
    {trash.data && (trash.data.total > 50 || offset > 0) && <div className="flex items-center justify-between px-4 py-4 text-sm">
      <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))} className="disabled:opacity-40">← Oldingi</button>
      <span>{offset + 1}–{Math.min(offset + 50, trash.data.total)} / {trash.data.total}</span>
      <button type="button" disabled={offset + 50 >= trash.data.total} onClick={() => setOffset(offset + 50)} className="disabled:opacity-40">Keyingi →</button>
    </div>}
  </Page>;
}
