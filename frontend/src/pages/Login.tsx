import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { AuthConfig } from "@/lib/types";

export default function Login() {
  const { state, magicError } = useAuth();

  const { data: config, isPending } = useQuery({
    queryKey: ["auth-config"],
    queryFn: async () => (await api.get<AuthConfig>("/auth/config")).data,
    staleTime: Infinity,
  });

  return (
    <main className="mx-auto flex min-h-full max-w-md flex-col justify-center gap-8 px-4 py-16">
      <header>
        <h1 className="text-3xl font-semibold tracking-tight">Vazifalar</h1>
        <p className="mt-2 text-muted">Jamoaning ichki vazifalar doskasi.</p>
      </header>

      {state.status === "pending" ? (
        <section className="rounded-lg border border-hairline bg-card p-4">
          <p className="font-medium">Hisobingiz hali tasdiqlanmagan.</p>
          <p className="mt-1 text-sm text-muted">
            Egasi botdagi so‘rovingizni tasdiqlashi kerak.
          </p>
        </section>
      ) : (
        <section className="flex flex-col gap-3">
          {isPending && <p className="text-sm text-muted">yuklanmoqda…</p>}
          {magicError && (
            <p className="text-sm text-late">Havola ishlatilgan yoki muddati tugagan. Botdan /login yozib yangisini oling.</p>
          )}
          {config?.login_enabled && <>
            <p className="text-muted">Botga /login yozing va u yuborgan bir martalik havolani oching.</p>
            <a className="rounded-lg bg-ink px-4 py-3 text-center font-semibold text-paper" href={`https://t.me/${config.bot_username}?start=login`}>
              Botni ochish
            </a>
          </>}
          {config && !config.login_enabled && (
            <p className="text-sm text-muted">
              Bot hali ulanmagan. Serverdagi env faylga BOT_USERNAME ni yozing.
            </p>
          )}
        </section>
      )}
    </main>
  );
}
