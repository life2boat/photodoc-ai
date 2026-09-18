export const METRIKA_COUNTER_ID = import.meta.env.VITE_YANDEX_METRIKA_ID
  ? String(import.meta.env.VITE_YANDEX_METRIKA_ID).trim()
  : null;

export function initMetrics() {
  if (!METRIKA_COUNTER_ID || METRIKA_COUNTER_ID === '12345678') {
    return;
  }

  try {
    if (typeof window !== 'undefined') {
      (function(m,e,t,r,i,k,a){
        m[i]=m[i]||function(){(m[i].a=m[i].a||[]).push(arguments)};
        m[i].l=1*new Date();
        for (let j = 0; j < document.scripts.length; j++) {
          if (document.scripts[j].src === r) { return; }
        }
        k=e.createElement(t);
        a=e.getElementsByTagName(t)[0];
        k.async=1;
        k.src=r;
        a.parentNode.insertBefore(k,a);
      })(window, document, "script", "https://mc.yandex.ru/metrika/tag.js", "ym");

      if (typeof window.ym === 'function') {
        window.ym(Number(METRIKA_COUNTER_ID), "init", {
          clickmap: true,
          trackLinks: true,
          accurateTrackBounce: true,
          webvisor: true
        });
      }
    }
  } catch (err) {
    console.warn('Yandex Metrika initialization error:', err);
  }
}

export function reachGoal(goalName) {
  try {
    if (METRIKA_COUNTER_ID && METRIKA_COUNTER_ID !== '12345678' && typeof window !== 'undefined' && typeof window.ym === 'function') {
      window.ym(Number(METRIKA_COUNTER_ID), 'reachGoal', goalName);
    } else {
      // In dev or without counter configured, quietly log
      console.info(`[Metrics] Goal skipped (no active counter): ${goalName}`);
    }
  } catch (error) {
    console.error('Metrics error:', error);
  }
}
