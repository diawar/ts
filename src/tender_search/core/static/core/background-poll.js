// Pause read-only HTMX polling in hidden tabs; resume without losing load chains.
// The draft extension queue is intentionally separate: it drives active work.
(() => {
  const active = new WeakSet();
  const isPoll = element => element?.hasAttribute('data-background-poll');
  function guard(event) {
    const element = event.detail.elt;
    if (isPoll(element) && (document.hidden || active.has(element))) event.preventDefault();
  }
  // confirm fires before HTMX's request queue; never inherit a sync policy
  // from a polling panel into the user's POST forms inside that panel.
  document.addEventListener('htmx:confirm', guard);
  document.addEventListener('htmx:beforeRequest', guard);
  document.addEventListener('htmx:configRequest', event => {
    if (isPoll(event.detail.elt)) event.detail.timeout = 20000;
  });
  document.addEventListener('htmx:beforeSend', event => {
    if (isPoll(event.detail.elt)) active.add(event.detail.elt);
  });
  document.addEventListener('htmx:afterRequest', event => active.delete(event.detail.elt));
  function resume() {
    if (document.hidden) return;
    for (const element of document.querySelectorAll('[data-background-poll][hx-get]')) {
      if (!active.has(element)) window.htmx?.trigger(element, 'app:resume');
    }
  }
  document.addEventListener('visibilitychange', resume);
  document.addEventListener('app:account-ready', resume);
  window.addEventListener('pageshow', event => { if (event.persisted) resume(); });
})();
