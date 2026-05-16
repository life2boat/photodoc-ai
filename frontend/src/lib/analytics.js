export const METRIKA_COUNTER_ID = 12345678;

export function reachGoal(goalName) {
  if (typeof window !== 'undefined' && typeof window.ym === 'function') {
    window.ym(METRIKA_COUNTER_ID, 'reachGoal', goalName);
  }
}
