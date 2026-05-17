export const METRIKA_COUNTER_ID = 12345678;

export function reachGoal(goalName) {
  try {
    if (typeof window !== 'undefined' && typeof window.ym === 'function') {
      window.ym(METRIKA_COUNTER_ID, 'reachGoal', goalName);
    } else {
      console.warn(`Metrics blocked or not loaded. Skipped goal: ${goalName}`);
    }
  } catch (error) {
    console.error('Metrics error:', error);
  }
}
