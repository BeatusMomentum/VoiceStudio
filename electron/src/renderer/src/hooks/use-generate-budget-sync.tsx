import { useEffect } from 'react';
import { isBackendReachable } from '@shared/utils/backendStage';
import { primeGenerateBudget } from '@/lib/api/generate-budget';
import { useBackendStatus } from './use-backend-status';

/** Re-reads the backend's generate budgets whenever the backend becomes reachable. */
export function GenerateBudgetSync(): null {
  const reachable = isBackendReachable(useBackendStatus().stage);
  useEffect(() => {
    if (reachable) void primeGenerateBudget();
  }, [reachable]);
  return null;
}
