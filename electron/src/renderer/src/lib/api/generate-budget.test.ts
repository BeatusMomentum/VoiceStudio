import { afterEach, describe, expect, it, vi } from 'vitest';
import { BACKEND_GENERATE_BUDGET_S, generateAbortMs } from '@shared/utils/generateBudget';
import {
  primeGenerateBudget,
  reportedGenerateBudget,
  resetGenerateBudgetForTests,
} from './generate-budget';

afterEach(() => {
  resetGenerateBudgetForTests();
  vi.unstubAllGlobals();
});

describe('generate budget', () => {
  it('outlasts budgets an operator raised on the backend', () => {
    const queueWait = BACKEND_GENERATE_BUDGET_S.queueWait + 3_600;
    expect(generateAbortMs(0, { queueWait })).toBe(generateAbortMs(0) + 3_600_000);
  });

  it('never drops below the built-in defaults or trusts malformed values', () => {
    expect(generateAbortMs(0, { queueWait: 1, modelLoad: 'x', executionBase: Number.NaN })).toBe(
      generateAbortMs(0),
    );
  });

  it('reads the backend report and keeps it when a later read fails', async () => {
    const body = {
      modelLoad: 5_000,
      queueWait: 1_800,
      executionBase: 900,
      progressExtensionCap: 1_800,
    };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(body))));
    await primeGenerateBudget();
    expect(reportedGenerateBudget()).toEqual(body);

    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));
    await primeGenerateBudget();
    expect(reportedGenerateBudget()).toEqual(body);
  });
});
