// Reads every visible form control on the page into fields (SPEC APP-9). Each control is tagged
// data-bw="<n>" so the filler finds the same element again. Radios and checkboxes are grouped by
// name; a group's options are their labels. Seen live (29 Oct 2026): Lever marks required with ✱
// after the label; Ashby draws yes/no questions as two buttons over a hidden checkbox, puts an
// "autofill from resume" upload above the form, names each checkbox after its option, and heads a
// fieldset with a <label> instead of a <legend>.
() => {
  const visible = e => { if (!e) return false; const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  // Web components keep their controls in shadow roots, which querySelectorAll doesn't enter.
  const deepAll = (sel, root = document) => {
    const out = [...root.querySelectorAll(sel)];
    for (const host of root.querySelectorAll('*')) if (host.shadowRoot) out.push(...deepAll(sel, host.shadowRoot));
    return out;
  };
  const rootOf = e => e.getRootNode && e.getRootNode().querySelector ? e.getRootNode() : document;
  const LABELISH = /label|title|question|legend|prompt/i;
  const nearestText = (e, levels = 6) => {
    let node = e;
    for (let i = 0; i < levels && node; i++) {
      node = node.parentElement;
      if (!node) break;
      const before = [...node.querySelectorAll('label, legend, h3, h4, p, span, div')]
        .filter(x => !x.contains(e) && !x.querySelector('input, textarea, select, button')
                && (x.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING) && clean(x.innerText));
      if (before.length) {
        const labelish = before.filter(x => x.tagName === 'LABEL' || x.tagName === 'LEGEND' || LABELISH.test(x.className));
        return clean((labelish.length ? labelish[labelish.length - 1] : before[before.length - 1]).innerText);
      }
    }
    return '';
  };
  // The browser's accessible-name order: aria-labelledby, aria-label, <label for>. Jotform gives one
  // input a question label and a sub-label; the aria attributes say which one names it.
  const AUTOCOMPLETE = { 'given-name': 'First name', 'family-name': 'Last name', email: 'Email', tel: 'Phone',
                         name: 'Full name', organization: 'Organisation' };
  const labelOf = e => {
    const ids = e.getAttribute && e.getAttribute('aria-labelledby');
    if (ids) { const t = ids.split(/\s+/).map(i => (rootOf(e).getElementById?.(i) || document.getElementById(i))?.innerText || '').join(' ');
               if (clean(t)) return clean(t); }
    if (e.getAttribute && e.getAttribute('aria-label')) return clean(e.getAttribute('aria-label'));
    const byFor = e.id && rootOf(e).querySelector(`label[for="${CSS.escape(e.id)}"]`);
    if (byFor && clean(byFor.innerText)) return clean(byFor.innerText);
    const wrap = e.closest && e.closest('label');
    if (wrap && clean(wrap.innerText)) return clean(wrap.innerText);
    const hint = AUTOCOMPLETE[((e.getAttribute && e.getAttribute('autocomplete')) || '').split(/\s+/).pop()];
    return hint || nearestText(e) || clean((e.getAttribute && e.getAttribute('placeholder')) || e.name || '');
  };
  const split = raw => {  // "Resume/CV ✱ ATTACH RESUME/CV" → ["Resume/CV", true]
    const m = raw.match(/^(.*?)\s*(?:✱|\*)(?:\s|$)/);
    if (m && m[1]) return [m[1].trim(), true];
    const req = /\(required\)/i.test(raw);
    return [raw.replace(/\(required\)/ig, '').trim().slice(0, 400), req];
  };
  const out = []; const seenGroups = new Set(); let n = Number(document.body.dataset.bwNext || 0);
  const tag = e => { if (!e.dataset.bw) e.dataset.bw = String(++n); return e.dataset.bw; };
  const push = (e, raw, f) => {
    const [label, starred] = split(raw);
    out.push({ ...f, name: '@' + tag(e), label, required: !!(f.required || starred) });
  };
  // The site's own furniture is not the form: search boxes, menus, newsletter sign-ups, cookie
  // banners (seen live on Meetup, Eventbrite and listing sites, 29 Oct 2026).
  const CHROME = 'header, nav, footer, aside, [role=search], [role=navigation], [role=banner], [role=contentinfo], [data-bw-skip]';
  const FURNITURE = /\bsearch\b|newsletter|subscribe|cookie|^sidebar$|by city or zip/i;
  // Google Forms draws choices as ARIA widgets: div role=radio / role=checkbox / role=listbox.
  for (const e of deepAll('input, textarea, select, [role=combobox], [contenteditable=true], [role=radio], [role=checkbox], [role=listbox]')) {
    const role = e.tagName === 'INPUT' ? '' : (e.getAttribute('role') || '');
    const type = role === 'radio' || role === 'checkbox' ? role
      : ['INPUT', 'SELECT', 'TEXTAREA'].includes(e.tagName) ? (e.type || '').toLowerCase() : '';  // a <button> dropdown is not "submit"
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search', 'password'].includes(type)) continue;
    if (e.closest(CHROME)) continue;
    const form = e.closest('form');
    if (FURNITURE.test(`${e.placeholder || ''} ${e.getAttribute('aria-label') || ''} ${e.name || ''} ${form?.action || ''} ${form?.id || ''} ${form?.className || ''}`)) continue;
    // Ashby: a yes/no question is two buttons; the checkbox under them is hidden.
    const yesno = type === 'checkbox' && e.parentElement?.querySelector('button[data-option]');
    if (yesno) {
      const box = e.parentElement;
      push(box, labelOf(box), { kind: 'select', widget: 'yesno',
        options: [...box.querySelectorAll('button[data-option]')].map(b => clean(b.innerText)),
        required: e.required || box.closest('[class*=required]') != null });
      continue;
    }
    if (type === 'radio' || type === 'checkbox') {
      const set = e.closest(role ? 'fieldset, [role=radiogroup], [role=group], [role=list]' : 'fieldset');
      const inSet = set ? [...set.querySelectorAll(role ? `[role=${type}]` : `input[type=${type}]`)] : [];
      if (inSet.length > 1 && seenGroups.has(set)) continue;
      const group = inSet.length > 1 ? set : (e.name || e.id || '');
      if (group && seenGroups.has(group)) continue;
      seenGroups.add(group);
      const members = inSet.length > 1 ? inSet
        : e.name ? [...rootOf(e).querySelectorAll(`input[name="${CSS.escape(e.name)}"]`)] : [e];
      if (!members.some(m => visible(m) || visible(m.closest('label')) || visible(m.parentElement))) continue;
      const box = members.length > 1
        ? (members[0].closest('fieldset, [role=radiogroup], [role=group]') || commonParent(members))
        : (members[0].closest('fieldset') || members[0].parentElement);
      const options = members.map(m => clean(role ? (m.getAttribute('aria-label') || m.dataset.value || m.dataset.answerValue || m.innerText)
        : (m.id && rootOf(m).querySelector(`label[for="${CSS.escape(m.id)}"]`)?.innerText)
          || m.closest('label')?.innerText || m.getAttribute('aria-label') || m.value).replace(/:$/, ''));
      const ids = new Set(members.map(m => m.id).filter(Boolean));
      const heading = [...box.querySelectorAll('legend, label')].find(x => !x.querySelector('input')
        && !(x.htmlFor && ids.has(x.htmlFor)) && !members.some(m => x.contains(m)) && clean(x.innerText));
      const legend = clean(heading?.innerText);
      members.forEach(tag);
      push(box, legend || labelOf(box), { kind: type === 'radio' ? 'select' : 'multiselect', widget: type, options,
        required: members.some(m => m.required) || box.getAttribute('aria-required') === 'true'
                  || box.closest('[role=listitem]')?.querySelector('[aria-label="Required question"]') != null,
        members: members.map(m => m.dataset.bw) });
      continue;
    }
    if (!visible(e) && type !== 'file') continue;
    // Google Forms: the "Other:" choice's text box lives inside the choice group; it is part of that choice.
    if (!role && e.closest('[role=radiogroup], [role=list]') && e.closest('[role=listitem]')?.querySelector('[role=radio], [role=checkbox]')) continue;
    if (type === 'file' && autofill(e)) continue;  // Ashby's "autofill from resume"
    let kind = 'text', widget = 'input', options = [];
    if (e.tagName === 'TEXTAREA' || e.isContentEditable) { kind = 'textarea'; widget = e.isContentEditable ? 'editable' : 'textarea'; }
    else if (role === 'listbox') { kind = 'select'; widget = 'listbox';
      options = [...e.querySelectorAll('[role=option]')].map(o => o.dataset.value || '').filter(Boolean); }
    else if (e.tagName === 'SELECT') { kind = e.multiple ? 'multiselect' : 'select'; widget = 'native';
      options = [...e.options].filter(o => o.value !== '' && !/^(-+|select|choose|please select)/i.test(o.text.trim()))
        .map(o => clean(o.text)); }
    else if (e.getAttribute('role') === 'combobox' && !['tel', 'email', 'number', 'url'].includes(type)) {  // Tally's phone box
      if (e.parentElement?.querySelector('select')) continue;  // Jotform: a styled face on a native select
      kind = 'select'; widget = 'combobox'; }
    else if (type === 'file') { kind = 'file'; widget = 'file'; }
    else if (type === 'date') { widget = 'date'; }
    // Google labels its email question in the viewer's language ("بريدك الإلكتروني").
    const named = labelOf(e);
    push(e, type === 'email' && !/mail/i.test(named) ? 'Email' : named, { kind, widget, options,
      required: e.required || e.getAttribute('aria-required') === 'true'
                || e.closest('[role=listitem]')?.querySelector('[aria-label="Required question"]') != null });
  }
  document.body.dataset.bwNext = String(n);
  return out;

  function autofill(e) {
    let p = e;
    for (let i = 0; i < 5 && p; i++) {
      p = p.parentElement;
      if (!p || p.querySelectorAll('input, textarea, select').length > 1) return false;  // only its own box
      if (/autofill/i.test(p.innerText || '')) return true;
    }
    return false;
  }

  function commonParent(nodes) {
    let p = nodes[0].parentElement;
    while (p && !nodes.every(x => p.contains(x))) p = p.parentElement;
    return p || document.body;
  }
}
