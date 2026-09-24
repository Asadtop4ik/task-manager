import { useCallback, useMemo, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { useAuth } from "@/lib/auth";
import { useHotkeys } from "@/lib/useHotkeys";
import NewTaskSheet from "@/components/NewTaskSheet";
import CommandPalette from "@/components/CommandPalette";

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
  const tabs = user?.is_owner
    ? [...TABS, { to: "/team", label: "Jamoa", end: false }]
    : TABS;
  const [composing, setComposing] = useState(false);
  const [searching, setSearching] = useState(false);

  const openCompose = useCallback(() => setComposing(true), []);
  const hotkeys = useMemo(
    () => ({
      "mod+k": () => setSearching(true),
      n: openCompose,
    }),
    [openCompose],
  );
  // Off while a sheet is open: ⌘K over a half-written task would bury it.
  useHotkeys(hotkeys, !composing && !searching);

  // Full width on purpose. Capping the whole shell left the rail floating in the
  // middle of a wide monitor with paper on both sides of it; the rail belongs
  // against the edge, and only the reading column inside is measured (see Page).
  return (
    <div className="flex min-h-screen w-full flex-col sm:flex-row">
      <nav className="order-2 sticky bottom-0 z-20 border-t border-hairline bg-card sm:order-1 sm:sticky sm:top-0 sm:h-screen sm:w-56 sm:shrink-0 sm:border-r sm:border-t-0">
        <div className="hidden px-5 py-6 sm:block">
          <p className="text-page font-semibold">Vazifalar</p>
          {user && <p className="mt-0.5 truncate text-sm text-muted">{user.full_name}</p>}
        </div>

        <ul className="flex sm:mt-1 sm:flex-col">
          {tabs.map((tab) => (
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
            onClick={() => setSearching(true)}
            className="flex w-full items-center justify-between text-sm text-muted"
          >
            Qidirish
            <kbd className="rounded border border-hairline px-1.5 py-0.5 text-xs">⌘K</kbd>
          </button>
          <button
            type="button"
            onClick={() => void logout()}
            className="mt-4 text-sm text-muted underline underline-offset-4"
          >
            Chiqish
          </button>
        </div>
      </nav>

      <main className="order-1 min-w-0 flex-1 sm:order-2">
        <Outlet />
      </main>

      {composing && <NewTaskSheet onClose={() => setComposing(false)} />}
      {searching && (
        <CommandPalette
          onClose={() => setSearching(false)}
          onNewTask={() => {
            setSearching(false);
            setComposing(true);
          }}
        />
      )}
    </div>
  );
}
