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
        <h1 className="text-3xl font-semibold tracking-tight">Vazifalar</h1>
        <p className="mt-2 text-muted">Ketoshop, QurBot va Kans Shop uchun.</p>
      </header>

      {state.status === "pending" ? (
        <section className="rounded-lg border border-hairline bg-card p-4">
          <p className="font-medium">Hisobingiz hali tasdiqlanmagan.</p>
          <p className="mt-1 text-sm text-muted">
            Kirdingiz, lekin menejer tasdiqlashi kerak. Tasdiqlangach sahifani yangilang.
          </p>
        </section>
      ) : (
        <section className="flex flex-col gap-3">
          {isPending && <p className="text-sm text-muted">yuklanmoqda…</p>}
          {config?.login_enabled && (
            <TelegramLoginButton botUsername={config.bot_username} onAuth={onAuth} />
          )}
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
