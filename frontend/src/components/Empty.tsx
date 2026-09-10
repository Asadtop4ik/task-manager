type Props = { title: string; hint?: string };

/** An empty screen is an invitation to act, not a shrug. */
export default function Empty({ title, hint }: Props) {
  return (
    <div className="px-4 py-10 text-center">
      <p className="font-medium">{title}</p>
      {hint && <p className="mx-auto mt-1 max-w-xs text-sm text-muted">{hint}</p>}
    </div>
  );
}
