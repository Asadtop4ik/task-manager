import Page from "@/components/Page";
import { useAuth } from "@/lib/auth";
import { useAgentMetrics } from "@/lib/queries";
import type { MetricDuration } from "@/lib/types";

function time(seconds: number | null): string {
  if (seconds === null) return "—";
  return seconds < 60 ? `${seconds} soniya` : `${(seconds / 60).toFixed(1)} daqiqa`;
}

function Metric({ label, value }: { label: string; value: MetricDuration }) {
  return (
    <article className="rounded-xl border border-hairline bg-card p-4">
      <p className="text-sm text-muted">{label}</p>
      <p className="mt-2 font-semibold text-ink">50% chegarasi: {time(value.p50_seconds)}</p>
      <p className="mt-1 text-sm text-muted">90% chegarasi: {time(value.p90_seconds)} · {value.samples} ta o‘lchov</p>
    </article>
  );
}

export default function AgentMetricsPage() {
  const { state } = useAuth();
  const owner = state.status === "authenticated" && state.user.is_owner;
  const { data, isLoading, isError } = useAgentMetrics(owner);
  if (!owner) return <Page><p className="p-4">Bunga ruxsatingiz yo‘q.</p></Page>;

  return (
    <Page>
      <div className="p-4 sm:p-6">
        <h1 className="text-2xl font-semibold text-ink">Agent tezligi</h1>
        <p className="mt-2 text-sm text-muted">Birinchi 20 ta haqiqiy taskning natijasi bosqichma-bosqich to‘planadi.</p>
        {isLoading && <p className="mt-6 text-muted">Yuklanmoqda…</p>}
        {isError && <p className="mt-6 text-late">O‘lchovlarni olib bo‘lmadi.</p>}
        {data && (
          <>
            <div className="mt-6 rounded-xl border border-hairline bg-card p-5">
              <p className="text-sm text-muted">Yig‘ilgan tasklar</p>
              <p className="mt-1 text-3xl font-semibold text-ink">{data.sampled_runs} / {data.target_tasks}</p>
              <p className="mt-2 text-sm text-muted">
                {data.enough_data ? "20 ta task bo‘yicha hisobot tayyor." : "Hisobot hali yig‘ilmoqda."}
                {" "}Boshlanish: {new Date(data.since).toLocaleString("uz-UZ", { timeZone: "Asia/Tashkent" })}
              </p>
            </div>
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <Metric label="Navbat kutish" value={data.queue} />
              <Metric label="Agentdan PRgacha" value={data.implementation} />
              <Metric label="PR inson ko‘rigida" value={data.human_review} />
              <Metric label="Taskdan productiongacha" value={data.end_to_end} />
            </div>
            <p className="mt-5 text-sm text-muted">
              Production’ga chiqqan: {data.deployed} · Xatoli urinish: {data.failed_attempts} ·
              Bekor qilingan: {data.cancelled_attempts} · Qayta urinish: {data.retried}
            </p>
            <p className="mt-2 text-sm text-muted">
              Codex tokenlari: {data.input_tokens.toLocaleString()} kirish,
              {" "}{data.cached_input_tokens.toLocaleString()} kesh,
              {" "}{data.output_tokens.toLocaleString()} chiqish. Bu obuna narxi emas.
            </p>
            <p className="mt-2 text-sm text-muted">
              CI minutlari va rollback hodisalari 20-task tekshiruvida GitHub Actions bilan solishtiriladi.
            </p>
            {data.enough_data && (data.queue.p90_seconds ?? 0) > 300 && (
              <p className="mt-4 rounded-lg border border-hairline p-3 text-sm text-ink">
                Navbatning 90% chegarasi 5 daqiqadan oshgan. Ikkinchi alohida runnerni ko‘rib chiqing.
              </p>
            )}
          </>
        )}
      </div>
    </Page>
  );
}
