import { useCallback } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { AuthConfig, TelegramWidgetUser } from "@/lib/types";
import TelegramLoginButton from "@/components/TelegramLoginButton";

export default function Login() {
  const { state, loginWithWidget } = useAuth();

  const { data: config, isPending } = useQuery({
    queryKey: ["auth-config"],
    queryFn: async () => (await api.get<AuthConfig>("/auth/config")).data,
    staleTime: Infinity,
  });

  const onAuth = useCallback(
    (user: TelegramWidgetUser) => {
      void loginWithWidget(user);
    },
    [loginWithWidget],
  );

  return (
    <main className="mx-auto flex min-h-full max-w-md flex-col justify-center gap-8 px-4 py-16">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Task Manager</h1>
        <p className="mt-2 text-sm opacity-70">
          Ketoshop, QurBot va Kans Shop uchun vazifalar.
        </p>
      </header>

      {state.status === "pending" ? (
        <section className="rounded-xl border border-amber-500/40 bg-amber-500/10 p-4 text-sm">
          <p className="font-medium">Hisobingiz tasdiqlanmagan.</p>
          <p className="mt-1 opacity-80">
            Kirdingiz, lekin menejer hisobingizni tasdiqlashi kerak. Tasdiqlangach shu
            sahifani yangilang.
          </p>
        </section>
      ) : (
        <section className="flex flex-col gap-3">
          {isPending && <p className="text-sm opacity-70">yuklanmoqda…</p>}
          {config?.login_enabled && (
            <TelegramLoginButton botUsername={config.bot_username} onAuth={onAuth} />
          )}
          {config && !config.login_enabled && (
            <p className="text-sm opacity-70">
              Bot sozlanmagan (BOT_USERNAME bo‘sh). Serverdagi env faylni to‘ldiring.
            </p>
          )}
        </section>
      )}
    </main>
  );
}
