import type { Project } from "@/lib/types";

/**
 * A project's key, in its own colour.
 *
 * The key — `keto`, `qur`, `kans` — is the name this team already uses: it is
 * what the bot's quick capture matches on and what the deploy scripts call the
 * stack. Showing it, rather than an invented monogram, means the label in the UI
 * and the word you type into Telegram are the same word.
 */
export default function ProjectTag({
  project,
  size = "sm",
}: {
  project: Pick<Project, "key" | "color">;
  size?: "sm" | "lg";
}) {
  return (
    <span
      className={[
        "inline-block shrink-0 rounded font-semibold",
        size === "lg" ? "px-2 py-1 text-[15px]" : "px-1.5 py-0.5 text-[13px]",
      ].join(" ")}
      style={{
        color: project.color,
        // Mixed rather than a second hard-coded tint per project, so a project
        // created next week gets a matching background for free.
        backgroundColor: `color-mix(in oklab, ${project.color} 16%, transparent)`,
      }}
    >
      {project.key}
    </span>
  );
}

/**
 * What a new project may be painted.
 *
 * No reds: red means late, and a project wearing it would make every one of its
 * rows read as urgent. Hues are spaced far enough apart to stay distinct at the
 * 3px width of a row rail.
 */
export const PROJECT_COLOURS = [
  "#10b981",
  "#f59e0b",
  "#6366f1",
  "#06b6d4",
  "#8b5cf6",
  "#84cc16",
  "#14b8a6",
  "#64748b",
];
