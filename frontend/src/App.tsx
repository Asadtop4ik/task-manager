import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "@/lib/auth";
import Shell from "@/components/Shell";
import Board from "@/pages/Board";
import Login from "@/pages/Login";
import MyDay from "@/pages/MyDay";
import ProjectDetail from "@/pages/ProjectDetail";
import Projects from "@/pages/Projects";
import TaskDetail from "@/pages/TaskDetail";
import Team from "@/pages/Team";

export default function App() {
  const { state } = useAuth();

  if (state.status === "loading") {
    return (
      <main className="flex min-h-screen items-center justify-center">
        <p className="text-muted">yuklanmoqda…</p>
      </main>
    );
  }

  // Pending and anonymous both land on Login; it says which one you are, because
  // "waiting for approval" and "log in" need different next steps.
  if (state.status !== "authenticated") return <Login />;

  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Shell />}>
          <Route index element={<MyDay />} />
          <Route path="board" element={<Board />} />
          <Route path="projects" element={<Projects />} />
          <Route path="projects/:id" element={<ProjectDetail />} />
          <Route path="tasks/:id" element={<TaskDetail />} />
          <Route path="team" element={<Team />} />
          {/* Deep links from the bot's "Ochish" button land here if the id is
              gone; send them somewhere useful rather than a blank screen. */}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
