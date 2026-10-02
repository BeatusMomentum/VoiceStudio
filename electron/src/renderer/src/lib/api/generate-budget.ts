import type { ReportedGenerateBudget } from '@shared/utils/generateBudget';
import { apiJson } from './client';

let reported: ReportedGenerateBudget = {};

/** The backend's active generate budgets, as last reported; empty until then. */
export function reportedGenerateBudget(): ReportedGenerateBudget {
  return reported;
}

/**
 * Read GET /generate/budget so the /generate backstop follows timeouts an
 * operator raised through the environment. Backends before 0.5.7 have no such
 * route; the built-in defaults then apply.
 */
export async function primeGenerateBudget(): Promise<void> {
  try {
    const body = await apiJson<ReportedGenerateBudget>('/generate/budget');
    if (body && typeof body === 'object') reported = body;
  } catch {
    // Keep the last known values.
  }
}

export function resetGenerateBudgetForTests(): void {
  reported = {};
}
