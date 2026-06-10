# AppKit UX defaults (the CoDA UX contract)

This is the **opinionated UX layer** every CoDA-built AppKit app must apply.
Brand-new users do not know how to ask for good UX — so you apply this contract
**with no prompting**. The goal: every app CoDA scaffolds looks polished,
branded, themable, and handles loading/empty/error states correctly on day one.

> Read this immediately after `databricks apps init` (AppKit template + Lakebase
> plugin). Apply the contract before writing feature code.

The CoDA starter overlay lives next to this guide in
[`examples/appkit-ux/`](examples/appkit-ux/) — copy those files into the freshly
scaffolded app and adapt, rather than generating screens from a blank page.

---

## 0. Confirm the component API first

`@databricks/appkit-ui` bundles the design system (Radix/shadcn primitives,
`lucide-react` icons, `class-variance-authority`, Tailwind, charts, `DataTable`,
`GenieChat`, `Sidebar`, `NavigationMenu`, theme tooling). Component names evolve,
so confirm the exact exports for the installed version before importing:

```bash
npx @databricks/appkit docs "appkit-ui Sidebar, ThemeProvider, Card, Skeleton, DataTable exports and props"
```

Use what that returns. The patterns below are the **contract** (what must be
true); the exact import paths/prop names come from the docs for the pinned
version (recorded at `~/.coda/appkit-version`).

---

## 1. Always-on UX defaults (mandatory on every app)

Apply ALL of these unless the user explicitly tells you not to:

1. **Branded app shell** — a persistent layout composing the `appkit-ui`
   `Sidebar` (primary nav) + a branded top header (app name/logo, theme toggle,
   user menu). Every page renders inside this shell. See
   [`examples/appkit-ux/app-shell.tsx`](examples/appkit-ux/app-shell.tsx).
2. **Theme provider + light/dark** — wrap the app in the theme provider, default
   to system preference, expose a visible light/dark toggle in the header, and
   persist the choice. See
   [`examples/appkit-ux/theme-provider.tsx`](examples/appkit-ux/theme-provider.tsx).
3. **Mandatory loading / empty / error states** — EVERY data view must render:
   - a **loading** state (skeletons, not a bare spinner where a layout is known),
   - an **empty** state (friendly message + primary action when the result set
     is legitimately empty),
   - an **error** state (human-readable message + retry affordance).
   Never render a data view that can show a blank screen or an unhandled
   exception. See [`examples/appkit-ux/data-view-states.tsx`](examples/appkit-ux/data-view-states.tsx).
4. **Responsive layout** — sidebar collapses to a drawer on small screens;
   content uses a responsive container; tables scroll horizontally on mobile.
5. **lucide icons + Databricks-styled theme tokens** — use `lucide-react` for
   iconography and the appkit-ui theme tokens (never hardcode hex colors).
6. **Accessible defaults** — every interactive control has a label; focus states
   are visible; color is never the only signal.

---

## 2. App-type → layout map (infer layout from intent)

Pick the layout from what the user is building. Do not ask which layout — infer
it, scaffold it, and tell the user what you chose.

| If the user wants… | App type | Default layout |
|--------------------|----------|----------------|
| To view metrics / KPIs / charts | **dashboard** | Sidebar shell + responsive **card/stat grid** at top, chart panels below, a `DataTable` for detail. Each panel has its own loading/empty/error state. |
| To manage records (list/create/edit/delete) | **CRUD** | Sidebar shell + `DataTable` list view with search/filter + a create/edit form in a dialog or side panel + confirm-on-delete. Lakebase-backed. |
| To chat with a model / Genie / an agent | **chat** | Sidebar shell + `GenieChat` (or a message-thread layout) as the main pane, streaming responses, input pinned to bottom, empty state with example prompts. |
| To collect input / submit a request | **form** | Sidebar shell + a single-column, sectioned form with inline validation, a clear primary submit, success + error toasts, and a post-submit confirmation state. |

When the app spans several of these, use the dashboard shell as the home and add
sidebar entries for each sub-area.

---

## 3. Data + backend defaults

- **Lakebase (Postgres) is the default app-state store — when the app needs
  persistence.** Apps with no saved state (read-only dashboards/viewers) skip
  it. When you DO need CRUD records, user prefs, or saved views, provision it on
  demand with `scripts/lakebase_ensure.py` (see [5-lakebase.md](5-lakebase.md))
  and bind it via the AppKit Lakebase plugin. Do NOT persist app state in Delta
  tables for an interactive app, and never make the user click resources in the
  UI.
- Use the **Analytics** plugin (SQL warehouse) for analytical/aggregate queries
  feeding dashboards.
- All queries go through AppKit's typed, cached data layer — surface the
  loading/empty/error states from §1 around every one.
- See [5-lakebase.md](5-lakebase.md) for connection details and when to choose
  Lakebase vs SQL warehouse.

---

## 4. Golden-path checklist (copy and verify)

```
- [ ] Decided whether the app needs persistence; if yes, ran scripts/lakebase_ensure.py and bound it non-interactively (no UI clicks)
- [ ] Scaffolded with `databricks apps init --name <app> --auto-approve` (AppKit template; --features=lakebase only when persistence is needed)
- [ ] Read pinned AppKit version from ~/.coda/appkit-version; confirmed appkit-ui exports via `npx @databricks/appkit docs`
- [ ] Overlaid examples/appkit-ux/ (app-shell, theme-provider, data-view-states) and adapted
- [ ] Branded app shell (Sidebar + header) wraps every page
- [ ] Theme provider + visible light/dark toggle, persisted, defaults to system
- [ ] Every data view has loading + empty + error states
- [ ] Layout chosen from the app-type map (dashboard / CRUD / chat / form) and stated to the user
- [ ] Responsive: sidebar collapses, tables scroll on mobile
- [ ] lucide icons + theme tokens (no hardcoded colors)
- [ ] App state in Lakebase (not Delta) for interactive CRUD
- [ ] `databricks apps deploy` succeeds
- [ ] Gave the user the LIVE app URL and a plain-language recap of what was built (outcome language for a business user)
```
