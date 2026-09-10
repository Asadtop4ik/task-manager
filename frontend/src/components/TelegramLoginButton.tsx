import { useEffect, useRef } from "react";
import type { TelegramWidgetUser } from "@/lib/types";

declare global {
  interface Window {
    onTelegramAuth?: (user: TelegramWidgetUser) => void;
  }
}

type Props = {
  botUsername: string;
  onAuth: (user: TelegramWidgetUser) => void;
};

/**
 * Telegram's Login Widget.
 *
 * It is a third-party script that renders its own iframe button, so it cannot be
 * expressed as JSX — the script tag has to be injected with the data-* attributes
 * already set, and it only calls back through a global function.
 *
 * The widget renders nothing at all unless the bot's domain is registered with
 * BotFather (`/setdomain`). A blank space here almost always means that step is
 * missing, not that the code is wrong.
 */
export default function TelegramLoginButton({ botUsername, onAuth }: Props) {
  const container = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const mount = container.current;
    if (!mount) return;

    window.onTelegramAuth = onAuth;

    const script = document.createElement("script");
    script.src = "https://telegram.org/js/telegram-widget.js?22";
    script.async = true;
    script.setAttribute("data-telegram-login", botUsername);
    script.setAttribute("data-size", "large");
    script.setAttribute("data-radius", "10");
    script.setAttribute("data-userpic", "true");
    script.setAttribute("data-request-access", "write");
    script.setAttribute("data-onauth", "onTelegramAuth(user)");
    mount.appendChild(script);

    return () => {
      mount.replaceChildren();
      delete window.onTelegramAuth;
    };
  }, [botUsername, onAuth]);

  return <div ref={container} className="min-h-[48px]" />;
}
