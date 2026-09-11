import { useEffect } from "react";

/** True when the keystroke belongs to whatever the person is typing into. */
function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return (
    target.isContentEditable ||
    ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)
  );
}

type Handlers = Record<string, (event: KeyboardEvent) => void>;

/**
 * Single-key shortcuts, plus `mod+key` for ⌘/Ctrl.
 *
 * A bare letter never fires while a field has focus — otherwise typing "n" into
 * a comment would open the new-task sheet, which is the classic way keyboard
 * shortcuts make an app feel hostile.
 */
export function useHotkeys(handlers: Handlers, enabled = true): void {
  useEffect(() => {
    if (!enabled) return;
    const onKey = (event: KeyboardEvent) => {
      const mod = event.metaKey || event.ctrlKey;
      const key = event.key.toLowerCase();
      const combo = mod ? `mod+${key}` : key;

      const handler = handlers[combo];
      if (!handler) return;
      if (!mod && key !== "escape" && isTyping(event.target)) return;

      event.preventDefault();
      handler(event);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [handlers, enabled]);
}
