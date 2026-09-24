import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { api } from "@/lib/api";
import type {
  Activity,
  AgentRun,
  Comment,
  Member,
  Project,
  Task,
  TaskList,
  TaskStatus,
  User,
} from "@/lib/types";

export type TaskFilters = {
  project_id?: number;
  assignee_id?: number;
  status?: TaskStatus[];
  open_only?: boolean;
  overdue?: boolean;
  q?: string;
  limit?: number;
};

export function useProjects(includeArchived = false) {
  return useQuery({
    queryKey: ["projects", { includeArchived }],
    queryFn: async () =>
      (await api.get<Project[]>("/projects", { params: { include_archived: includeArchived } }))
        .data,
    staleTime: 5 * 60_000,
  });
}

export function useCreateProject() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (payload: { key: string; name: string; color: string }) =>
      (await api.post<Project>("/projects", payload)).data,
    onSuccess: () => void client.invalidateQueries({ queryKey: ["projects"] }),
  });
}

export function useUpdateProject() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, ...patch }: { id: number } & Partial<Project>) =>
      (await api.patch<Project>(`/projects/${id}`, patch)).data,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["projects"] });
      // Tasks carry an embedded copy of the project, so a rename or a recolour
      // has to reach every list too.
      void client.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
}

export function useMembers(projectId: number) {
  return useQuery({
    queryKey: ["members", projectId],
    queryFn: async () => (await api.get<Member[]>(`/projects/${projectId}/members`)).data,
  });
}

export function useAddMember() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ projectId, userId }: { projectId: number; userId: number }) =>
      (await api.put<Member>(`/projects/${projectId}/members/${userId}`)).data,
    onSuccess: (_data, variables) =>
      void client.invalidateQueries({ queryKey: ["members", variables.projectId] }),
  });
}

export function useRemoveMember() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ projectId, userId }: { projectId: number; userId: number }) => {
      await api.delete(`/projects/${projectId}/members/${userId}`);
    },
    onSuccess: (_data, variables) =>
      void client.invalidateQueries({ queryKey: ["members", variables.projectId] }),
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
      (await api.get<TaskList>("/tasks", {
        params: { limit: 100, ...filters },
        // FastAPI expects repeated `status` keys for a list, without brackets.
        paramsSerializer: { indexes: null },
      })).data,
  });
}

export function useTrash(offset = 0, limit = 50) {
  return useQuery({
    queryKey: ["trash", offset, limit],
    queryFn: async () =>
      (await api.get<TaskList>("/tasks/trash", { params: { offset, limit } })).data,
  });
}

export function useDeleteTask() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (id: number) => (await api.delete<Task>(`/tasks/${id}`)).data,
    onSuccess: (_task, id) => {
      void client.invalidateQueries({ queryKey: ["tasks"] });
      void client.invalidateQueries({ queryKey: ["trash"] });
      void client.invalidateQueries({ queryKey: ["task", id] });
    },
  });
}

export function useRestoreTask() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (id: number) => (await api.post<Task>(`/tasks/${id}/restore`)).data,
    onSuccess: (_task, id) => {
      void client.invalidateQueries({ queryKey: ["tasks"] });
      void client.invalidateQueries({ queryKey: ["trash"] });
      void client.invalidateQueries({ queryKey: ["task", id] });
    },
  });
}

export function useTask(id: number) {
  return useQuery({
    queryKey: ["task", id],
    queryFn: async () => (await api.get<Task>(`/tasks/${id}`)).data,
  });
}

export function useAgentRuns(taskId: number) {
  return useQuery({
    queryKey: ["agent-runs", taskId],
    queryFn: async () =>
      (await api.get<AgentRun[]>(`/agent-runs/tasks/${taskId}`)).data,
    refetchInterval: (query) => {
      const statuses = query.state.data?.map((run) => run.status) ?? [];
      if (statuses.some((status) => ["pending", "dispatching", "dispatched", "running", "validating", "publishing", "deploying"].includes(status))) return 10_000;
      if (statuses.some((status) => ["pr_ready", "merged"].includes(status))) return 30_000;
      return false;
    },
  });
}

export function useStartAgentRun() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (taskId: number) =>
      (await api.post<AgentRun>(`/agent-runs/tasks/${taskId}`)).data,
    onSettled: (_data, _error, taskId) =>
      void client.invalidateQueries({ queryKey: ["agent-runs", taskId] }),
  });
}

export function useCancelAgentRun() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ runId }: { taskId: number; runId: string }) =>
      (await api.post<AgentRun>(`/agent-runs/${runId}/cancel`)).data,
    onSettled: (_data, _error, variables) =>
      void client.invalidateQueries({ queryKey: ["agent-runs", variables.taskId] }),
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

/**
 * Move a card within its column.
 *
 * Optimistic like the status change: the card is already under the cursor when
 * you let go, and watching it jump back for a round trip is what makes a board
 * feel like a form.
 */
export function useReorder() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({
      id,
      previous_id,
      next_id,
    }: {
      id: number;
      previous_id?: number | null;
      next_id?: number | null;
    }) => (await api.post<Task>(`/tasks/${id}/reorder`, { previous_id, next_id })).data,
    onMutate: async ({ id, previous_id, next_id }) => {
      await client.cancelQueries({ queryKey: ["tasks"] });
      const snapshot = client.getQueriesData<TaskList>({ queryKey: ["tasks"] });
      // Board columns may come from separate API pages. Look up neighbours
      // across all cached task lists before changing the dragged card's rank.
      const byId = new Map(
        snapshot.flatMap(([, value]) => (value?.items ?? []).map((task) => [task.id, task] as const)),
      );
      const previous = previous_id ? byId.get(previous_id) : undefined;
      const following = next_id ? byId.get(next_id) : undefined;
      let position: number | undefined;
      if (previous && following) position = (previous.position + following.position) / 2;
      else if (previous) position = previous.position + 1024;
      else if (following) position = following.position - 1024;
      if (position === undefined) return { snapshot };
      for (const [key, value] of snapshot) {
        if (!value) continue;
        client.setQueryData<TaskList>(key, {
          ...value,
          items: value.items.map((task) => (task.id === id ? { ...task, position } : task)),
        });
      }
      return { snapshot };
    },
    onError: (_error, _variables, context) => {
      for (const [key, value] of context?.snapshot ?? []) client.setQueryData(key, value);
    },
    onSettled: () => void client.invalidateQueries({ queryKey: ["tasks"] }),
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
