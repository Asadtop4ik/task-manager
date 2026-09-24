import { useState } from "react";
import { useAuth } from "@/lib/auth";
import { api } from "@/lib/api";
import { useUsers } from "@/lib/queries";
import Page from "@/components/Page";

export default function Team() {
  const { state } = useAuth();
  const members = useUsers();
  const [saving, setSaving] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const owner = state.status === "authenticated" ? state.user : null;

  if (!owner?.is_owner) return <Page><p className="p-4">Bunga ruxsatingiz yo‘q.</p></Page>;

  async function setAccess(userId: number, enabled: boolean) {
    setSaving(userId);
    setError(null);
    try {
      await api.put(`/team/members/${userId}/codex-access`, { enabled });
      await members.refetch();
    } catch {
      setError("Codex huquqini yangilab bo‘lmadi. Faqat bitta sherikka ruxsat beriladi.");
    } finally {
      setSaving(null);
    }
  }

  return <Page>
    <header className="px-4 pt-8 pb-5">
      <h1 className="text-page font-semibold">Jamoa</h1>
      <p className="mt-2 text-sm text-muted">Yangi sherik uchun botga /invite yozing. U taklifni ochgach, tasdiqlash xabari sizga keladi.</p>
    </header>
    {error && <p role="alert" className="mx-4 mb-4 text-sm text-late">{error}</p>}
    <ul className="divide-y divide-hairline border-y border-hairline bg-card">
      {members.data?.map((member) => <li key={member.id} className="flex items-center justify-between gap-3 px-4 py-4">
        <div>
          <p className="font-medium">{member.full_name}{member.id === owner.id ? " · egasi" : ""}</p>
          {member.username && <p className="text-sm text-muted">@{member.username}</p>}
        </div>
        <label className="flex items-center gap-2 text-sm">
          Codex
          <input
            type="checkbox"
            checked={member.id === owner.id || member.can_use_codex}
            disabled={member.id === owner.id || saving !== null}
            onChange={(event) => void setAccess(member.id, event.target.checked)}
          />
        </label>
      </li>)}
    </ul>
  </Page>;
}
