import { useEffect, useRef, type ReactNode } from "react";

/**
 * A bottom sheet on a phone, a centred panel from sm up.
 *
 * Shared by every "add one thing" flow so they behave identically: Escape
 * closes, the backdrop closes, the panel itself does not, and focus lands on the
 * first field rather than on the page behind it.
 */
export default function Sheet({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    // Without this the page behind scrolls under the sheet on a phone.
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    panel.current?.querySelector<HTMLElement>("input, select, textarea")?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previous;
    };
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-30 flex items-end justify-center bg-black/45 sm:items-center"
      onClick={onClose}
      role="presentation"
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(event) => event.stopPropagation()}
        className="max-h-[90vh] w-full max-w-md overflow-y-auto rounded-t-2xl border border-hairline bg-card p-5 sm:rounded-2xl"
      >
        <h2 className="text-page font-semibold">{title}</h2>
        {children}
      </div>
    </div>
  );
}

export const fieldClass =
  "mt-1 w-full rounded-lg border border-hairline bg-paper px-3 py-2.5 text-base";
export const labelClass = "block text-sm font-medium";
export const primaryButton =
  "rounded-lg bg-ink px-4 py-2.5 text-sm font-semibold text-paper disabled:opacity-40";
export const quietButton = "rounded-lg px-3 py-2.5 text-sm font-medium text-muted";
