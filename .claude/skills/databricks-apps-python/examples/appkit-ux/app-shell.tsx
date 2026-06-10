// CoDA UX overlay — branded app shell.
// Reference template: confirm exact @databricks/appkit-ui exports for the
// pinned version (`npx @databricks/appkit docs`) and reconcile imports.
//
// Contract: a persistent layout composing the appkit-ui Sidebar (primary nav)
// + a branded top header (app name/logo, theme toggle, user menu). EVERY page
// renders inside <AppShell>. Sidebar collapses to a drawer on small screens.

import { LayoutDashboard, Table2, MessageSquare, FileText } from "lucide-react";
// Confirm these exports exist for the installed appkit-ui version.
import { Sidebar, SidebarItem } from "@databricks/appkit-ui";
import { ThemeToggle } from "./theme-provider";

export type NavItem = {
  label: string;
  href: string;
  icon: React.ComponentType<{ className?: string }>;
};

// Tailor these to the app's areas — one entry per dashboard / CRUD / chat /
// form sub-area (see the app-type -> layout map in 7-appkit-ux.md).
export const NAV_ITEMS: NavItem[] = [
  { label: "Overview", href: "/", icon: LayoutDashboard },
  { label: "Records", href: "/records", icon: Table2 },
  { label: "Assistant", href: "/assistant", icon: MessageSquare },
  { label: "Requests", href: "/requests", icon: FileText },
];

export function AppShell({
  appName = "My App",
  children,
}: {
  appName?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex h-screen w-full overflow-hidden bg-background text-foreground">
      {/* Primary nav — collapses to a drawer on small screens. */}
      <Sidebar aria-label="Primary navigation">
        {NAV_ITEMS.map(({ label, href, icon: Icon }) => (
          <SidebarItem key={href} href={href}>
            <Icon className="h-4 w-4" />
            <span>{label}</span>
          </SidebarItem>
        ))}
      </Sidebar>

      <div className="flex min-w-0 flex-1 flex-col">
        {/* Branded header */}
        <header className="flex h-14 items-center justify-between border-b px-4">
          <div className="flex items-center gap-2 font-semibold">
            {/* Replace with the app logo if available */}
            <span className="text-lg">{appName}</span>
          </div>
          <div className="flex items-center gap-2">
            <ThemeToggle />
            {/* User menu — wire to the Databricks identity from request headers */}
          </div>
        </header>

        {/* Responsive content container */}
        <main className="flex-1 overflow-auto">
          <div className="mx-auto w-full max-w-7xl p-4 md:p-6">{children}</div>
        </main>
      </div>
    </div>
  );
}
