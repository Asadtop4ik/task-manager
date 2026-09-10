import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Activity, Comment, Project, Task, TaskList, TaskStatus, User } from "@/lib/types";

export type TaskFilters = {
  project_id?: number;
  assignee_id?: number;
  status?: TaskStatus[];
  open_only?: boolean;
  overdue?: boolean;
  q?: string;
  limit?: number;
};

export function useProjects() {
  return useQuery({
    queryKey: ["projects"],
    queryFn: async () => (await api.get<Project[]>("/projects")).data,
    staleTime: 5 * 60_000,
  });
}

export function useUsers() {
  return useQuery({
    queryKey: ["users"],
    queryFn: async () => (await api.get<User[]>("/users")).data,
    staleTime: 5 * 60_000,
  });
}

export function useTasks(filters: TaskFilters) {
  return useQuery({
    queryKey: ["tasks", filters],
    queryFn: async () =>
      (await api.get<TaskList>("/tasks", { params: { limit: 100, ...filters } })).data,
  });
}

export function useTask(id: number) {
  return useQuery({
    queryKey: ["task", id],
    queryFn: async () => (await api.get<Task>(`/tasks/${id}`)).data,
  });
}

export function useComments(id: number) {
  return useQuery({
    queryKey: ["comments", id],
    queryFn: async () => (await api.get<Comment[]>(`/tasks/${id}/comments`)).data,
  });
}

export function useActivity(id: number) {
  return useQuery({
    queryKey: ["activity", id],
    queryFn: async () => (await api.get<Activity[]>(`/tasks/${id}/activity`)).data,
  });
}

/**
 * Invalidate everything that can show a task.
 *
 * One helper rather than a list at each call site: a mutation that forgets one
 * key leaves a stale row on some other screen, and that bug is invisible until
 * someone notices the board disagreeing with My Day.
 */
function invalidateTask(client: QueryClient, id: number) {
  void client.invalidateQueries({ queryKey: ["tasks"] });
  void client.invalidateQueries({ queryKey: ["task", id] });
  void client.invalidateQueries({ queryKey: ["activity", id] });
}

/**
 * Move a task, showing the result before the server confirms.
 *
 * Dragging a card and watching it snap back for 300ms is the difference between
 * a board that feels like an app and one that feels like a form. On failure the
 * snapshot is restored, so a refused transition puts the card back where it was.
 */
export function useTransition() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, status }: { id: number; status: TaskStatus }) =>
      (await api.post<Task>(`/tasks/${id}/transition`, { status })).data,
    onMutate: async ({ id, status }) => {
      await client.cancelQueries({ queryKey: ["tasks"] });
      const snapshot = client.getQueriesData<TaskList>({ queryKey: ["tasks"] });
      for (const [key, value] of snapshot) {
        if (!value) continue;
        client.setQueryData<TaskList>(key, {
          ...value,
          items: value.items.map((task) => (task.id === id ? { ...task, status } : task)),
        });
      }
      return { snapshot };
    },
    onError: (_error, _variables, context) => {
      for (const [key, value] of context?.snapshot ?? []) client.setQueryData(key, value);
    },
    onSettled: (_data, _error, variables) => invalidateTask(client, variables.id),
  });
}

export function useUpdateTask() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, ...patch }: { id: number } & Partial<Task> & Record<string, unknown>) =>
      (await api.patch<Task>(`/tasks/${id}`, patch)).data,
    onSettled: (_data, _error, variables) => invalidateTask(client, variables.id),
  });
}

export function useAssign() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, assignee_id }: { id: number; assignee_id: number | null }) =>
      (await api.post<Task>(`/tasks/${id}/assign`, { assignee_id })).data,
    onSettled: (_data, _error, variables) => invalidateTask(client, variables.id),
  });
}

export function useLogTime() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, minutes }: { id: number; minutes: number }) =>
      (await api.post<Task>(`/tasks/${id}/time`, { minutes })).data,
    onSettled: (_data, _error, variables) => invalidateTask(client, variables.id),
  });
}

export function useAddComment() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, body }: { id: number; body: string }) =>
      (await api.post<Comment>(`/tasks/${id}/comments`, { body })).data,
    onSuccess: (_data, variables) => {
      void client.invalidateQueries({ queryKey: ["comments", variables.id] });
      void client.invalidateQueries({ queryKey: ["activity", variables.id] });
    },
  });
}

export function useCreateTask() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (payload: Record<string, unknown>) =>
      (await api.post<Task>("/tasks", payload)).data,
    onSuccess: () => void client.invalidateQueries({ queryKey: ["tasks"] }),
  });
}
