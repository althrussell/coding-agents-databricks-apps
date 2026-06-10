// CoDA UX overlay — mandatory loading / empty / error states.
// Reference template: confirm exact @databricks/appkit-ui exports for the
// pinned version (`npx @databricks/appkit docs`) and reconcile imports.
//
// Contract: EVERY data view renders a loading state (skeletons over a known
// layout), an empty state (message + primary action), and an error state
// (human-readable message + retry). Never let a data view show a blank screen
// or an unhandled exception.

import { AlertCircle, Inbox, RefreshCw } from "lucide-react";
import { Button, Skeleton } from "@databricks/appkit-ui";

type AsyncState<T> = {
  data: T | undefined;
  isLoading: boolean;
  error: unknown;
  isEmpty?: boolean; // optional override; defaults to "data is empty array/null"
  onRetry?: () => void;
};

function defaultIsEmpty(data: unknown): boolean {
  if (data == null) return true;
  if (Array.isArray(data)) return data.length === 0;
  return false;
}

/**
 * Wrap any data view. Pass the async state; render children only on success.
 *
 *   <DataView state={query} loadingRows={5} emptyAction={<Button>Add</Button>}>
 *     {(rows) => <DataTable rows={rows} />}
 *   </DataView>
 */
export function DataView<T>({
  state,
  children,
  loadingRows = 4,
  emptyTitle = "Nothing here yet",
  emptyDescription = "There's no data to show.",
  emptyAction,
}: {
  state: AsyncState<T>;
  children: (data: T) => React.ReactNode;
  loadingRows?: number;
  emptyTitle?: string;
  emptyDescription?: string;
  emptyAction?: React.ReactNode;
}) {
  if (state.isLoading) {
    return (
      <div className="space-y-3" aria-busy="true" aria-live="polite">
        {Array.from({ length: loadingRows }).map((_, i) => (
          <Skeleton key={i} className="h-10 w-full" />
        ))}
      </div>
    );
  }

  if (state.error) {
    return (
      <div
        role="alert"
        className="flex flex-col items-center gap-3 rounded-lg border border-destructive/30 p-8 text-center"
      >
        <AlertCircle className="h-8 w-8 text-destructive" />
        <div>
          <p className="font-medium">Something went wrong</p>
          <p className="text-sm text-muted-foreground">
            {state.error instanceof Error ? state.error.message : String(state.error)}
          </p>
        </div>
        {state.onRetry && (
          <Button variant="outline" onClick={state.onRetry}>
            <RefreshCw className="mr-2 h-4 w-4" /> Retry
          </Button>
        )}
      </div>
    );
  }

  const empty = state.isEmpty ?? defaultIsEmpty(state.data);
  if (empty) {
    return (
      <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed p-8 text-center">
        <Inbox className="h-8 w-8 text-muted-foreground" />
        <div>
          <p className="font-medium">{emptyTitle}</p>
          <p className="text-sm text-muted-foreground">{emptyDescription}</p>
        </div>
        {emptyAction}
      </div>
    );
  }

  return <>{children(state.data as T)}</>;
}
