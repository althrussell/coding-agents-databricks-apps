# CoDA AppKit UX starter overlay

These are **reference templates**, not a standalone project. After you scaffold
an AppKit app (`databricks apps init` → AppKit template + Lakebase plugin), copy
these files into the app's `src/` and adapt them. They encode the CoDA UX
contract from [`../../7-appkit-ux.md`](../../7-appkit-ux.md):

| File | Purpose |
|------|---------|
| `theme-provider.tsx` | Theme context + light/dark toggle, defaults to system, persists choice |
| `app-shell.tsx` | Branded app shell: `appkit-ui` Sidebar + top header (logo, theme toggle, user menu); every page renders inside it |
| `data-view-states.tsx` | Reusable loading / empty / error wrappers — wrap EVERY data view |
| `dashboard-page.tsx` | Example dashboard page: stat-card grid + chart + Lakebase-wired `DataTable`, all with proper states |

## Before you copy

The exact `@databricks/appkit-ui` export names and props evolve per version.
Confirm them for the installed (pinned) version first:

```bash
npx @databricks/appkit docs "appkit-ui exports: Sidebar, ThemeProvider, Card, Skeleton, DataTable, Button, NavigationMenu"
```

Then reconcile the imports in these templates with what the docs return. Treat
the **structure** (shell wraps pages, every data view has 3 states, layout
chosen from the app-type map) as the contract; treat the exact import paths as
version-specific details to confirm.
