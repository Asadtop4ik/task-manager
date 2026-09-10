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
  created_at: string;
};

export type Project = {
  id: number;
  key: string;
  name: string;
  color: string;
  is_archived: boolean;
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
  estimate_minutes: number | null;
  spent_minutes: number;
  source: "bot" | "web";
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
