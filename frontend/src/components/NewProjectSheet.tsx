import { useState } from "react";
import { AxiosError } from "axios";
import { useCreateProject } from "@/lib/queries";
import Sheet, { fieldClass, labelClass, primaryButton, quietButton } from "@/components/Sheet";
import ProjectTag, { PROJECT_COLOURS } from "@/components/ProjectTag";

/** `keto`, `kans-shop` — lowercase, no spaces, because the bot parses it. */
function toKey(name: string): string {
  return name
    .toLowerCase()
    .replace(/[‘’']/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 32);
}

export default function NewProjectSheet({ onClose }: { onClose: () => void }) {
  const create = useCreateProject();
  const [name, setName] = useState("");
  const [key, setKey] = useState("");
  const [touchedKey, setTouchedKey] = useState(false);
  const [color, setColor] = useState(PROJECT_COLOURS[0]!);

  // The key follows the name until someone edits it, then it is theirs.
  const effectiveKey = touchedKey ? key : toKey(name);
  const valid = name.trim().length > 0 && /^[a-z0-9][a-z0-9-]{1,31}$/.test(effectiveKey);

  const conflict =
    create.error instanceof AxiosError && create.error.response?.status === 409;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!valid || create.isPending) return;
    await create.mutateAsync({ key: effectiveKey, name: name.trim(), color });
    onClose();
  }

  return (
    <Sheet title="Yangi loyiha" onClose={onClose}>
      <form onSubmit={submit}>
        <label className={`mt-4 ${labelClass}`}>
          Nomi
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Ketoshop"
            className={fieldClass}
          />
        </label>

        <label className={`mt-4 ${labelClass}`}>
          Kalit
          <input
            value={effectiveKey}
            onChange={(event) => {
              setTouchedKey(true);
              setKey(toKey(event.target.value));
            }}
            placeholder="ketoshop"
            className={fieldClass}
          />
        </label>
        <p className="mt-1.5 text-sm text-muted">
          Botda shu kalitni yozasiz: <code>{effectiveKey || "keto"}: matn</code>. Faqat
          kichik harf, raqam va chiziqcha.
        </p>

        <fieldset className="mt-4">
          <legend className={labelClass}>Rangi</legend>
          <div className="mt-2 flex flex-wrap gap-2">
            {PROJECT_COLOURS.map((option) => (
              <button
                key={option}
                type="button"
                aria-label={`Rang ${option}`}
                aria-pressed={option === color}
                onClick={() => setColor(option)}
                className={[
                  "size-8 rounded-lg border-2",
                  option === color ? "border-ink" : "border-transparent",
                ].join(" ")}
                style={{ backgroundColor: option }}
              />
            ))}
          </div>
          <p className="mt-2 text-sm text-muted">
            Rang ro‘yxatlarda loyihani ajratib turadi. Qizil yo‘q — u kechikkan
            vazifalarniki.
          </p>
        </fieldset>

        <div className="mt-5 flex items-center gap-2 rounded-lg border border-hairline bg-paper px-3 py-2.5">
          <span className="w-[3px] self-stretch rounded-full" style={{ backgroundColor: color }} />
          <ProjectTag project={{ key: effectiveKey || "kalit", color }} />
          <span className="truncate font-medium">{name.trim() || "Loyiha nomi"}</span>
        </div>

        {conflict && (
          <p className="mt-3 text-sm text-late">
            <code>{effectiveKey}</code> kaliti band. Boshqa kalit tanlang.
          </p>
        )}
        {create.isError && !conflict && (
          <p className="mt-3 text-sm text-late">Saqlanmadi. Qaytadan urinib ko‘ring.</p>
        )}

        <div className="mt-5 flex justify-end gap-2">
          <button type="button" onClick={onClose} className={quietButton}>
            Bekor qilish
          </button>
          <button type="submit" disabled={!valid || create.isPending} className={primaryButton}>
            {create.isPending ? "Saqlanmoqda…" : "Loyiha qo‘shish"}
          </button>
        </div>
      </form>
    </Sheet>
  );
}
