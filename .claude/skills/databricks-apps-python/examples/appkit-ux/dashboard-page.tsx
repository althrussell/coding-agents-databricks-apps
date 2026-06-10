// CoDA UX overlay — example dashboard page.
// Reference template: confirm exact @databricks/appkit-ui exports for the
// pinned version (`npx @databricks/appkit docs`) and reconcile imports.
//
// Demonstrates the "dashboard" entry of the app-type -> layout map:
//   - responsive stat-card grid at the top
//   - a chart
//   - a Lakebase-wired DataTable below
// Every data region is wrapped in <DataView> for loading/empty/error states,
// and the whole page renders inside <AppShell>.

import { Activity, DollarSign, Users } from "lucide-react";
import { Card, DataTable } from "@databricks/appkit-ui";
import { AppShell } from "./app-shell";
import { DataView } from "./data-view-states";

// Replace with the app's real data hook. AppKit's Lakebase plugin exposes a
// typed client/route; this stands in for "a query that returns {data, isLoading,
// error, refetch}". Confirm the hook shape in `npx @databricks/appkit docs`.
type Row = { id: string; name: string; status: string; updatedAt: string };
declare function useRecords(): {
  data: Row[] | undefined;
  isLoading: boolean;
  error: unknown;
  refetch: () => void;
};

const STATS = [
  { label: "Active users", value: "1,284", icon: Users },
  { label: "Revenue", value: "$48.2k", icon: DollarSign },
  { label: "Events today", value: "9,310", icon: Activity },
];

function StatCard({
  label,
  value,
  icon: Icon,
}: {
  label: string;
  value: string;
  icon: React.ComponentType<{ className?: string }>;
}) {
  return (
    <Card className="flex items-center gap-4 p-4">
      <div className="rounded-md bg-muted p-2">
        <Icon className="h-5 w-5 text-muted-foreground" />
      </div>
      <div>
        <p className="text-sm text-muted-foreground">{label}</p>
        <p className="text-2xl font-semibold">{value}</p>
      </div>
    </Card>
  );
}

export default function DashboardPage() {
  const records = useRecords();

  return (
    <AppShell appName="Operations">
      <div className="space-y-6">
        <h1 className="text-2xl font-semibold tracking-tight">Overview</h1>

        {/* Responsive stat grid: 1 col on mobile, up to 3 on desktop. */}
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {STATS.map((s) => (
            <StatCard key={s.label} {...s} />
          ))}
        </div>

        {/* Chart region — swap in the appkit-ui chart component. */}
        <Card className="p-4">
          <h2 className="mb-4 text-lg font-medium">Trend</h2>
          {/* <AreaChart data={...} /> — confirm the chart export name. */}
          <div className="h-64 rounded-md bg-muted/40" />
        </Card>

        {/* Lakebase-backed table with full loading/empty/error handling. */}
        <Card className="p-4">
          <h2 className="mb-4 text-lg font-medium">Records</h2>
          <DataView
            state={{
              data: records.data,
              isLoading: records.isLoading,
              error: records.error,
              onRetry: records.refetch,
            }}
            loadingRows={6}
            emptyTitle="No records yet"
            emptyDescription="Records will appear here as they're created."
          >
            {(rows) => (
              <DataTable
                data={rows}
                columns={[
                  { accessorKey: "name", header: "Name" },
                  { accessorKey: "status", header: "Status" },
                  { accessorKey: "updatedAt", header: "Updated" },
                ]}
              />
            )}
          </DataView>
        </Card>
      </div>
    </AppShell>
  );
}
