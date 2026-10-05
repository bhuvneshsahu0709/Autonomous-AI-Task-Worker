/**
 * Page -> agent-readable snapshot.
 *
 * Raw HTML is the wrong input for an LLM: it is mostly markup, it blows the
 * context window, and CSS selectors invented from it are brittle. Instead we
 * hand the model two things:
 *
 *   1. an enumerated list of elements it can actually act on, each stamped with
 *      a short ref (`e7`) written into the DOM as `data-agentref`, and
 *   2. the page's rendered text.
 *
 * The agent then acts by ref ("click e7"), never by CSS selector. That removes a
 * whole class of failure (hallucinated selectors) and keeps observations small.
 *
 * Refs are regenerated on every snapshot and are only valid for the most recent
 * one - acting on a stale ref produces an explicit, recoverable error.
 */
(() => {
  const MAX_ELEMENTS = 150;
  const MAX_TEXT_CHARS = 4000;
  const MAX_NAME = 120;
  const MAX_CONTEXT = 150;

  const INTERACTIVE = [
    'a[href]',
    'button',
    'input:not([type="hidden"])',
    'select',
    'textarea',
    'summary',
    '[role="button"]',
    '[role="link"]',
    '[role="tab"]',
    '[onclick]',
  ].join(',');

  const squash = (s) => (s || '').replace(/\s+/g, ' ').trim();

  function isVisible(el) {
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function roleOf(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'summary') return 'disclosure';
    if (tag === 'select') return 'select';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (['submit', 'button', 'reset', 'image'].includes(t)) return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      // Called out separately so the agent never echoes a secret into a
      // visible field, and so the snapshot never carries its value.
      if (t === 'password') return 'password';
      return 'textbox';
    }
    return tag;
  }

  /** Accessible-name-ish: what a person would call this control. */
  function nameOf(el) {
    const aria = el.getAttribute('aria-label');
    if (aria) return squash(aria);

    if (el.id) {
      try {
        const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (lab && squash(lab.innerText)) return squash(lab.innerText);
      } catch (e) { /* malformed id - fall through */ }
    }

    const wrapping = el.closest('label');
    if (wrapping && wrapping !== el && squash(wrapping.innerText)) {
      return squash(wrapping.innerText);
    }

    const direct = squash(el.innerText);
    if (direct) return direct;

    return squash(
      el.getAttribute('placeholder') ||
      el.getAttribute('title') ||
      el.getAttribute('alt') ||
      el.getAttribute('name') ||
      ''
    );
  }

  /**
   * Nearest meaningful container text. This is what lets the model tell three
   * identical "View invoice" links apart - each carries its own row's text.
   */
  function contextOf(el) {
    const anc = el.closest('tr, li, fieldset');
    if (!anc || anc === el) return '';
    const text = squash(anc.innerText);
    if (!text || text === squash(el.innerText)) return '';
    return text.length > MAX_CONTEXT ? text.slice(0, MAX_CONTEXT) + '…' : text;
  }

  // Clear refs from the previous snapshot so numbering never drifts.
  document.querySelectorAll('[data-agentref]').forEach((e) => e.removeAttribute('data-agentref'));

  const elements = [];
  let counter = 0;
  let truncated = false;

  for (const el of document.querySelectorAll(INTERACTIVE)) {
    if (elements.length >= MAX_ELEMENTS) { truncated = true; break; }
    if (!isVisible(el)) continue;

    counter += 1;
    const ref = 'e' + counter;
    el.setAttribute('data-agentref', ref);

    const item = { ref: ref, role: roleOf(el), name: nameOf(el).slice(0, MAX_NAME) };

    const ctx = contextOf(el);
    if (ctx) item.context = ctx;

    if (el.tagName === 'A' && el.getAttribute('href')) {
      item.href = el.getAttribute('href');
    }
    if ('value' in el && el.type !== 'password') {
      const v = squash(String(el.value || ''));
      if (v) item.value = v.slice(0, 80);
    }
    if (el.disabled) item.disabled = true;
    if (el.checked !== undefined && (el.type === 'checkbox' || el.type === 'radio')) {
      item.checked = !!el.checked;
    }
    if (el.tagName === 'SUMMARY') {
      const details = el.closest('details');
      item.expanded = !!(details && details.open);
    }
    if (el.tagName === 'SELECT') {
      item.options = Array.from(el.options).slice(0, 25).map((o) => o.value);
    }

    elements.push(item);
  }

  let text = (document.body ? document.body.innerText : '') || '';
  text = text.replace(/\n{3,}/g, '\n\n').trim();
  const textTruncated = text.length > MAX_TEXT_CHARS;
  if (textTruncated) text = text.slice(0, MAX_TEXT_CHARS);

  return {
    url: window.location.href,
    title: document.title,
    elements: elements,
    elementsTruncated: truncated,
    text: text,
    textTruncated: textTruncated,
  };
})()
