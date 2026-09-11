import { useEffect, useRef, useState } from "react";

/**
 * Click the text, edit it where it sits, no dialog.
 *
 * Enter saves a single-line field and Escape always abandons the edit, so a
 * mistyped title is undone by the key people already reach for. Nothing is sent
 * when the value has not actually changed.
 */
export default function InlineText({
  value,
  onSave,
  multiline = false,
  placeholder,
  className = "",
  label,
}: {
  value: string;
  onSave: (next: string) => void;
  multiline?: boolean;
  placeholder?: string;
  className?: string;
  label: string;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const ref = useRef<HTMLTextAreaElement | HTMLInputElement>(null);

  useEffect(() => {
    if (!editing) setDraft(value);
  }, [value, editing]);

  useEffect(() => {
    if (editing) ref.current?.focus();
  }, [editing]);

  function commit() {
    const next = draft.trim();
    setEditing(false);
    if (next && next !== value) onSave(next);
    else setDraft(value);
  }

  if (!editing) {
    return (
      <button
        type="button"
        onClick={() => setEditing(true)}
        aria-label={`${label} — tahrirlash`}
        className={[
          "-mx-1 w-full rounded px-1 text-left hover:bg-ink/[0.04] dark:hover:bg-ink/[0.07]",
          value ? "" : "text-muted",
          className,
        ].join(" ")}
      >
        {value || placeholder}
      </button>
    );
  }

  const shared = {
    value: draft,
    onChange: (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
      setDraft(event.target.value),
    onBlur: commit,
    onKeyDown: (event: React.KeyboardEvent) => {
      if (event.key === "Escape") {
        setDraft(value);
        setEditing(false);
      }
      if (event.key === "Enter" && !multiline) commit();
    },
    "aria-label": label,
    className: `-mx-1 w-full rounded border border-hairline bg-paper px-1 ${className}`,
  };

  return multiline ? (
    <textarea {...shared} ref={ref as React.Ref<HTMLTextAreaElement>} rows={4} />
  ) : (
    <input {...shared} ref={ref as React.Ref<HTMLInputElement>} />
  );
}
