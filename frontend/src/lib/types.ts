export type Role = "manager" | "executor";

export type TaskStatus =
  | "backlog"
  | "todo"
  | "in_progress"
  | "blocked"
  | "review"
  | "done"
  | "cancelled";

export type TaskPriority = "low" | "normal" | "high" | "urgent";

export type User = {
  id: number;
  telegram_id: number;
  username: string | null;
  full_name: string;
  role: Role;
  lang: string;
  tz: string;
  is_active: boolean;
  can_use_codex: boolean;
  is_owner: boolean;
  created_at: string;
};

export type Project = {
  id: number;
  key: string;
  name: string;
  color: string;
  is_archived: boolean;
  repo_full_name: string | null;
  default_branch: string | null;
};

export type AgentRun = {
  run_id: string;
  task_id: number;
  repo_full_name: string;
  status: "pending" | "dispatching" | "dispatched" | "running" | "validating" | "publishing" | "deploying" | "pr_ready" | "merged" | "failed" | "deployed" | "cancelled";
  mode: "pr" | "fast";
  github_run_url: string | null;
  pr_url: string | null;
  head_sha: string | null;
  merged_sha: string | null;
  deployed_sha: string | null;
  error: string | null;
  attempts: number;
  attempt_index: number;
  input_tokens: number | null;
  cached_input_tokens: number | null;
  output_tokens: number | null;
  created_at: string;
  finished_at: string | null;
  runner_started_at: string | null;
  pr_ready_at: string | null;
  merged_at: string | null;
  deployed_at: string | null;
};

export type MetricDuration = {
  samples: number;
  p50_seconds: number | null;
  p90_seconds: number | null;
};

export type AgentMetrics = {
  since: string;
  target_tasks: number;
  sampled_runs: number;
  enough_data: boolean;
  deployed: number;
  failed_attempts: number;
  cancelled_attempts: number;
  retried: number;
  queue: MetricDuration;
  implementation: MetricDuration;
  human_review: MetricDuration;
  end_to_end: MetricDuration;
  input_tokens: number;
  cached_input_tokens: number;
  output_tokens: number;
};

export type Task = {
  id: number;
  project: Project;
  title: string;
  description: string | null;
  status: TaskStatus;
  priority: TaskPriority;
  assignee: User | null;
  created_by: User | null;
  due_at: string | null;
  started_at: string | null;
  done_at: string | null;
  deleted_at: string | null;
  estimate_minutes: number | null;
  spent_minutes: number;
  source: "bot" | "web";
  position: number;
  created_at: string;
  updated_at: string;
};

export type TaskList = {
  items: Task[];
  total: number;
  limit: number;
  offset: number;
};

export type AuthConfig = {
  bot_username: string;
  login_enabled: boolean;
};

export type TelegramWidgetUser = {
  id: number;
  first_name: string;
  last_name?: string;
  username?: string;
  photo_url?: string;
  auth_date: number;
  hash: string;
};

export type Comment = {
  id: number;
  body: string;
  author: User | null;
  created_at: string;
};

export type Activity = {
  id: number;
  kind:
    | "created"
    | "assigned"
    | "status_changed"
    | "priority_changed"
    | "due_changed"
    | "commented"
    | "time_logged"
    | "attached";
  payload: Record<string, unknown>;
  actor: User | null;
  created_at: string;
};

export type Member = {
  user: User;
  role_in_project: string;
};
