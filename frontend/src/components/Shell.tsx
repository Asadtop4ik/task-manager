import { NavLink, Outlet } from "react-router-dom";
import { useAuth } from "@/lib/auth";

const TABS = [
  { to: "/", label: "Bugun", end: true },
  { to: "/board", label: "Doska", end: false },
  { to: "/projects", label: "Loyihalar", end: false },
];

/**
 * Navigation follows the device.
 *
 * A bottom bar on a phone, where the thumb is, and a side rail from `sm` up.
 * This app is read standing up more often than sitting down, so the phone case
 * is the one that gets the ergonomic layout rather than a hamburger.
 */
export default function Shell() {
  const { state, logout } = useAuth();
  const user = state.status === "authenticated" ? state.user : null;

  return (
    <div className="mx-auto flex min-h-screen w-full max-w-5xl flex-col sm:flex-row">
      <nav className="order-2 sticky bottom-0 z-10 border-t border-hairline bg-card sm:order-1 sm:sticky sm:top-0 sm:h-screen sm:w-48 sm:shrink-0 sm:border-r sm:border-t-0">
        <div className="hidden px-4 py-5 sm:block">
          <p className="text-lg font-semibold tracking-tight">Vazifalar</p>
          {user && <p className="mt-0.5 truncate text-sm text-muted">{user.full_name}</p>}
        </div>

        <ul className="flex sm:flex-col">
          {TABS.map((tab) => (
            <li key={tab.to} className="flex-1">
              <NavLink
                to={tab.to}
                end={tab.end}
                className={({ isActive }) =>
                  [
                    "block px-4 py-3 text-center text-sm sm:text-left",
                    isActive
                      ? "font-semibold text-ink sm:border-l-2 sm:border-ink sm:pl-[14px]"
                      : "text-muted",
                  ].join(" ")
                }
              >
                {tab.label}
              </NavLink>
            </li>
          ))}
        </ul>

        <div className="hidden px-4 py-4 sm:block">
          <button
            type="button"
            onClick={() => void logout()}
            className="text-sm text-muted underline underline-offset-4"
          >
            Chiqish
          </button>
        </div>
      </nav>

      <main className="order-1 min-w-0 flex-1 pb-4 sm:order-2">
        <Outlet />
      </main>
    </div>
  );
}
