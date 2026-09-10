import { useAuth } from "@/lib/auth";
import Home from "@/pages/Home";
import Login from "@/pages/Login";

export default function App() {
  const { state } = useAuth();

  if (state.status === "loading") {
    return (
      <main className="flex min-h-full items-center justify-center">
        <p className="text-sm opacity-70">yuklanmoqda…</p>
      </main>
    );
  }

  return state.status === "authenticated" ? <Home /> : <Login />;
}
