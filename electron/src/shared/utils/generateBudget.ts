/**
 * Client backstop for a single /generate request.
 *
 * The backend bounds every phase of a generate itself and answers with a
 * descriptive error when one runs out, so the client abort exists only for a
 * backend that has gone silent. It must therefore outlast the backend's
 * longest legitimate budget: aborting first reports a failure while the job is
 * still running (and later lands in history).
 *
 * The values mirror the backend defaults in backend/services/model_manager.py
 * and are held equal by tests/test_generate_abort_budget.py:
 *
 * - modelLoad: cold-load ceiling, OMNIVOICE_MODEL_LOAD_TIMEOUT
 * - queueWait: GPU queue wait, OMNIVOICE_GPU_QUEUE_TIMEOUT_S
 * - executionBase: the largest default execution base (sidecar receive
 *   timeouts reach 900 s; CPU hosts get 600 s, accelerated hosts 300 s)
 * - sidecarGrace: the grace generate_timeout_s adds for sidecar engines
 * - progressExtensionCap / progressExtensionBudgets: heartbeat extension,
 *   max(OMNIVOICE_PROGRESS_EXTENSION_CAP_S, budgets x execution budget)
 * - freeChars / charsPerSecond: the execution budget's length scaling
 *
 * Operators who raise those budgets through the environment are beyond what
 * the client can know; the margin covers scheduling jitter only.
 */
export const BACKEND_GENERATE_BUDGET_S = {
  modelLoad: 1200,
  queueWait: 1800,
  executionBase: 900,
  sidecarGrace: 5,
  progressExtensionCap: 1800,
  progressExtensionBudgets: 3,
  freeChars: 1200,
  charsPerSecond: 40,
} as const;

const CLIENT_MARGIN_S = 60;

/** Milliseconds before the client gives up on a /generate for this text. */
export function generateAbortMs(textLength = 0): number {
  const budget = BACKEND_GENERATE_BUDGET_S;
  const execution =
    budget.executionBase +
    budget.sidecarGrace +
    Math.max(0, textLength - budget.freeChars) / budget.charsPerSecond;
  const extension = Math.max(
    budget.progressExtensionCap,
    budget.progressExtensionBudgets * execution,
  );
  return Math.ceil(
    (budget.modelLoad + budget.queueWait + execution + extension + CLIENT_MARGIN_S) * 1000,
  );
}
