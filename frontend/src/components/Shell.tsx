import { useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { useAuth } from "@/lib/auth";
import NewTaskSheet from "@/components/NewTaskSheet";

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
 * gets the ergonomic layout rather than a hamburger.
 *
 * "Yangi vazifa" lives in the nav rather than on one page, because the thought
 * arrives while you are looking at something else.
 */
export default function Shell() {
  const { state, logout } = useAuth();
  const user = state.status === "authenticated" ? state.user : null;
  const [composing, setComposing] = useState(false);

  return (
    <div className="mx-auto flex min-h-screen w-full max-w-5xl flex-col sm:flex-row">
      <nav className="order-2 sticky bottom-0 z-20 border-t border-hairline bg-card sm:order-1 sm:sticky sm:top-0 sm:h-screen sm:w-52 sm:shrink-0 sm:border-r sm:border-t-0">
        <div className="hidden px-5 py-6 sm:block">
          <p className="text-page font-semibold">Vazifalar</p>
          {user && <p className="mt-0.5 truncate text-sm text-muted">{user.full_name}</p>}
        </div>

        <ul className="flex sm:mt-1 sm:flex-col">
          {TABS.map((tab) => (
            <li key={tab.to} className="flex-1">
              <NavLink
                to={tab.to}
                end={tab.end}
                className={({ isActive }) =>
                  [
                    "block px-5 py-3.5 text-center text-sm sm:text-left",
                    isActive
                      ? "font-semibold text-ink sm:border-l-2 sm:border-ink sm:pl-[18px]"
                      : "text-muted",
                  ].join(" ")
                }
              >
                {tab.label}
              </NavLink>
            </li>
          ))}
          <li className="flex-1 sm:mt-3 sm:px-5">
            <button
              type="button"
              onClick={() => setComposing(true)}
              className="block w-full px-5 py-3.5 text-center text-sm font-semibold text-ink sm:rounded-lg sm:bg-ink sm:px-3 sm:py-2 sm:text-paper"
            >
              <span className="sm:hidden">+ Yangi</span>
              <span className="hidden sm:inline">Yangi vazifa</span>
            </button>
          </li>
        </ul>

        <div className="hidden px-5 py-5 sm:block">
          <button
            type="button"
            onClick={() => void logout()}
            className="text-sm text-muted underline underline-offset-4"
          >
            Chiqish
          </button>
        </div>
      </nav>

      <main className="order-1 min-w-0 flex-1 sm:order-2">
        <Outlet />
      </main>

      {composing && <NewTaskSheet onClose={() => setComposing(false)} />}
    </div>
  );
}
