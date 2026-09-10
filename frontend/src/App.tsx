import { useQuery } from "@tanstack/react-query";
import { fetchReady } from "@/lib/api";

function StatusPill({ label, state }: { label: string; state: string }) {
  const ok = state === "ok";
  return (
    <span
      className={`inline-flex items-center gap-2 rounded-full px-3 py-1 text-sm font-medium ${
        ok
          ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
          : "bg-red-500/10 text-red-700 dark:text-red-400"
      }`}
    >
      <span className={`size-2 rounded-full ${ok ? "bg-emerald-500" : "bg-red-500"}`} />
      {label}: {state}
    </span>
  );
}

export default function App() {
  const { data, isPending, isError } = useQuery({
    queryKey: ["ready"],
    queryFn: fetchReady,
    refetchInterval: 30_000,
  });

  return (
    <main className="mx-auto flex min-h-full max-w-2xl flex-col justify-center gap-6 px-4 py-12">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Task Manager</h1>
        <p className="mt-1 text-sm opacity-70">
          Skeleton — milestone 1. The board lands in milestone 4.
        </p>
      </div>

      <section className="rounded-xl border border-black/10 p-4 dark:border-white/15">
        <h2 className="text-sm font-medium uppercase tracking-wide opacity-60">Backend</h2>
        <div className="mt-3 flex flex-wrap gap-2">
          {isPending && <span className="text-sm opacity-70">checking…</span>}
          {isError && <StatusPill label="api" state="unreachable" />}
          {data &&
            Object.entries(data.checks).map(([name, state]) => (
              <StatusPill key={name} label={name} state={state} />
            ))}
        </div>
      </section>
    </main>
  );
}
