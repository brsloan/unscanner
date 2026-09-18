/* remediate UI: plain JS, no build step. Talks to the JSON API in webapp.py.
   Collaboration: the UI reports what it shows to /api/session/view, polls the document every 2 s
   so edits made by Claude (through the MCP server) appear, follows show_page requests, and refuses
   to overwrite a page that changed underneath it (HTTP 409). */
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = { docs: [], doc: null, page: null, pageData: null, dirty: false, job: null, settings: null,
  lastRequestAt: 0, lastSelection: "", polling: null };

// ------------------------------------------------------------------ api
async function api(path, opts = {}) {
  const res = await fetch("/api" + path, {
    headers: opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body && !(opts.body instanceof FormData) ? JSON.stringify(opts.body) : opts.body,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) { /* ignore */ }
    const err = new Error(msg); err.status = res.status; throw err;
  }
  return res.json();
}

function setStatus(text, isError = false) {
  const el = $("#status");
  el.textContent = text;
  el.classList.toggle("error", isError);
  // Show the expand toggle only when the message does not fit on one line.
  const bar = $("#statusbar");
  bar.classList.remove("expanded");
  $("#btn-status-expand").setAttribute("aria-expanded", "false");
  $("#btn-status-expand").textContent = "▲";
  requestAnimationFrame(() => { $("#btn-status-expand").hidden = el.scrollWidth <= el.clientWidth; });
}
$("#btn-status-expand").addEventListener("click", (e) => {
  const bar = $("#statusbar"), open = !bar.classList.contains("expanded");
  bar.classList.toggle("expanded", open);
  e.target.setAttribute("aria-expanded", String(open));
  e.target.textContent = open ? "▼" : "▲";
});

// ------------------------------------------------------------------ page image scaling (fit / fill / manual)
const img = $("#page-image"), scroller = $("#image-scroll");
state.zoomMode = localStorage.getItem("remediate.zoomMode") || "fit";
function applyZoom() {
  if (!img.naturalWidth) return;
  const cw = scroller.clientWidth - 12, ch = scroller.clientHeight - 12;
  let scale;
  if (state.zoomMode === "fit") scale = Math.min(cw / img.naturalWidth, ch / img.naturalHeight);
  else if (state.zoomMode === "fill") scale = cw / img.naturalWidth;
  else scale = (+$("#zoom").value / 100) * (cw / img.naturalWidth);
  img.style.width = Math.max(40, Math.floor(img.naturalWidth * scale)) + "px";
  $("#btn-fit").setAttribute("aria-pressed", String(state.zoomMode === "fit"));
  $("#btn-fill").setAttribute("aria-pressed", String(state.zoomMode === "fill"));
}
function setZoomMode(mode) {
  state.zoomMode = mode; localStorage.setItem("remediate.zoomMode", mode);
  if (mode !== "manual") $("#zoom").value = 100;
  applyZoom();
}
img.addEventListener("load", () => { applyZoom(); if (state.lastBox && (state.follow || state.mark)) placeMarker(state.lastBox); });
new ResizeObserver(applyZoom).observe(scroller);
$("#btn-fit").addEventListener("click", () => setZoomMode("fit"));
$("#btn-fill").addEventListener("click", () => setZoomMode("fill"));
$("#zoom").addEventListener("input", () => { state.zoomMode = "manual"; localStorage.setItem("remediate.zoomMode", "manual"); applyZoom(); });

// ------------------------------------------------------------------ follow / mark: locate the editor caret on the scan
const marker = $("#marker"), edgeTicks = $("#edge-ticks");
state.follow = localStorage.getItem("remediate.follow") === "true";
state.mark = localStorage.getItem("remediate.mark") === "true";
function renderFollowButtons() {
  $("#btn-follow").setAttribute("aria-pressed", String(state.follow));
  $("#btn-mark").setAttribute("aria-pressed", String(state.mark));
  if (!state.mark) { marker.hidden = true; edgeTicks.hidden = true; }
}

/* Ticks on the pane edges at the dot's column (top/bottom) and row (left/right); each pair is hidden
   when the dot is scrolled out of the pane in that direction. */
function updateEdgeTicks() {
  if (marker.hidden) { edgeTicks.hidden = true; return; }
  edgeTicks.hidden = false;  // must be laid out to be measured
  const area = edgeTicks.getBoundingClientRect(), m = marker.getBoundingClientRect();
  const x = m.left - area.left, y = m.top - area.top;
  const inX = x >= 0 && x <= area.width, inY = y >= 0 && y <= area.height;
  edgeTicks.hidden = !(inX || inY);
  for (const t of edgeTicks.querySelectorAll(".n, .s")) { t.hidden = !inX; t.style.left = x + "px"; }
  for (const t of edgeTicks.querySelectorAll(".w, .e")) { t.hidden = !inY; t.style.top = y + "px"; }
}
$("#image-scroll").addEventListener("scroll", updateEdgeTicks);
new ResizeObserver(updateEdgeTicks).observe($("#image-scroll"));
marker.addEventListener("transitionend", updateEdgeTicks);
$("#btn-follow").addEventListener("click", () => { state.follow = !state.follow; localStorage.setItem("remediate.follow", state.follow); renderFollowButtons(); locateCaret(); });
$("#btn-mark").addEventListener("click", () => { state.mark = !state.mark; localStorage.setItem("remediate.mark", state.mark); renderFollowButtons(); locateCaret(); });
renderFollowButtons();

const BLOCK_SEL = "p,li,h1,h2,h3,h4,h5,h6,td,th,dd,dt,figcaption,caption,blockquote,pre,aside";

/* Editor text built from its text nodes (a space between blocks) plus the caret's offset in it. */
function editorTextAndCaret(ed, anchorNode, anchorOffset) {
  let target = anchorNode, tOff = anchorOffset;
  if (target && target.nodeType !== Node.TEXT_NODE) {  // caret given as (element, child index): use the first text node from there
    const child = target.childNodes[anchorOffset] || target.childNodes[anchorOffset - 1] || target;
    const w = document.createTreeWalker(child, NodeFilter.SHOW_TEXT);
    target = child.nodeType === Node.TEXT_NODE ? child : w.nextNode();
    tOff = 0;
  }
  const walker = document.createTreeWalker(ed, NodeFilter.SHOW_TEXT);
  let text = "", caret = -1, prevBlock = null, node;
  while ((node = walker.nextNode())) {
    const block = (node.parentElement && node.parentElement.closest(BLOCK_SEL)) || ed;
    if (prevBlock && block !== prevBlock) text += " ";
    prevBlock = block;
    if (node === target) caret = text.length + tOff;
    text += node.data;
  }
  return { text, caret: caret < 0 ? text.length : caret };
}

/* Words around the caret in the editor: returns {context: [...], index} or null. */
function caretContext(before = 4, after = 4) {
  const sel = window.getSelection();
  const ed = $("#editor");
  if (!sel || !sel.anchorNode || !ed.contains(sel.anchorNode) || ed.hidden) return null;
  const { text, caret } = editorTextAndCaret(ed, sel.anchorNode, sel.anchorOffset);
  const re = /\S+/g; const words = []; let m;
  while ((m = re.exec(text))) words.push({ t: m[0], s: m.index, e: m.index + m[0].length });
  if (!words.length) return null;
  let i = words.findIndex((w) => caret <= w.e);
  if (i < 0) i = words.length - 1;
  const lo = Math.max(0, i - before), hi = Math.min(words.length, i + after + 1);
  return { context: words.slice(lo, hi).map((w) => w.t), index: i - lo };
}

/* Reverse direction: click a word on the scan to jump to it in the editor. */
const pageWordsCache = {};
async function pageWords() {
  const key = state.doc.doc_id + "/" + state.page;
  if (!pageWordsCache[key]) pageWordsCache[key] = await api(`/documents/${state.doc.doc_id}/pages/${state.page}/words`);
  return pageWordsCache[key];
}
const normTok = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, "");
function tokMatch(a, b) {
  if (!a || !b) return false;
  if (a === b) return true;
  const [s, l] = a.length <= b.length ? [a, b] : [b, a];
  return s.length >= 4 && l.startsWith(s);
}
/* Same alignment as locate.py: slide `context` over `words`, return the index aligned with context[index]. */
function alignWords(words, context, index) {
  const q = context.map(normTok), need = Math.min(3, q.filter(Boolean).length);
  let best = 0, bestPos = -1;
  for (let start = -index; start < words.length - index; start++) {
    let score = 0;
    for (let j = 0; j < q.length; j++) { const k = start + j; if (q[j] && k >= 0 && k < words.length && tokMatch(q[j], words[k])) score++; }
    const pos = start + index;
    const bonus = pos >= 0 && pos < words.length && tokMatch(q[index], words[pos]) ? 0.5 : 0;
    if (score + bonus > best && pos >= 0 && pos < words.length) { best = score + bonus; bestPos = pos; }
  }
  return best >= need ? bestPos : -1;
}
/* Editor words with their character ranges, plus the text-node segments to map a range back to the DOM. */
function editorWords(ed) {
  const walker = document.createTreeWalker(ed, NodeFilter.SHOW_TEXT);
  let text = "", prevBlock = null, node; const segs = [];
  while ((node = walker.nextNode())) {
    const block = (node.parentElement && node.parentElement.closest(BLOCK_SEL)) || ed;
    if (prevBlock && block !== prevBlock) text += " ";
    prevBlock = block;
    segs.push({ node, start: text.length, end: text.length + node.data.length });
    text += node.data;
  }
  const re = /\S+/g; const words = []; let m;
  while ((m = re.exec(text))) words.push({ t: m[0], s: m.index, e: m.index + m[0].length });
  const point = (off) => { const seg = segs.find((g) => off >= g.start && off <= g.end) || segs[segs.length - 1]; return seg ? [seg.node, Math.min(seg.node.data.length, off - seg.start)] : null; };
  return { words, point };
}
async function jumpToScanWord(evt) {
  if (state.suppressClick) { state.suppressClick = false; return; }
  if (!state.doc || !state.page || !img.naturalWidth || $("#toggle-source").checked) return;
  const rect = img.getBoundingClientRect();
  const px = (evt.clientX - rect.left) / rect.width * 1000, py = (evt.clientY - rect.top) / rect.height * 1000;
  if (px < 0 || px > 1000 || py < 0 || py > 1000) return;
  // A click inside a figure's crop box selects that figure in the editor.
  const fig = (state.pageData.figures || []).find((f) => f.bbox && px >= f.bbox[0] && px <= f.bbox[2] && py >= f.bbox[1] && py <= f.bbox[3]);
  if (fig) {
    const imgEl = $("#editor").querySelector(`img[data-fig="${CSS.escape(fig.id)}"]`);
    if (!imgEl) { setStatus(`Figure ${fig.id} is not in the transcription for this page.`); return; }
    const ed = $("#editor"), ir = imgEl.getBoundingClientRect(), er = ed.getBoundingClientRect();
    ed.scrollTop = ed.scrollTop + (ir.top - er.top) - er.height / 2 + ir.height / 2;
    selectFigure(imgEl);
    return;
  }
  let words;
  try { words = await pageWords(); } catch (_) { return; }
  // Word under the pointer, else the nearest word on that line, else the nearest word within reach.
  let i = words.findIndex((w) => px >= w.x0 && px <= w.x1 && py >= w.y0 && py <= w.y1);
  if (i < 0) {
    let bestD = 30;
    words.forEach((w, k) => {
      const onLine = py >= w.y0 && py <= w.y1;
      const dx = px < w.x0 ? w.x0 - px : px > w.x1 ? px - w.x1 : 0;
      const dy = py < w.y0 ? w.y0 - py : py > w.y1 ? py - w.y1 : 0;
      const d = onLine ? dx : Math.hypot(dx, dy) + 10;
      if (d < bestD) { bestD = d; i = k; }
    });
  }
  if (i < 0) return;
  const lo = Math.max(0, i - 4), hi = Math.min(words.length, i + 5);
  const context = words.slice(lo, hi).map((w) => w.t || w.text);
  const ed = $("#editor");
  const { words: ew, point } = editorWords(ed);
  const j = alignWords(ew.map((w) => normTok(w.t)), context, i - lo);
  if (j < 0) { setStatus(`"${words[i].text}" not found in the transcription for this page.`); return; }
  const a = point(ew[j].s), b = point(ew[j].e);
  if (!a || !b) return;
  const range = document.createRange(); range.setStart(a[0], a[1]); range.setEnd(b[0], b[1]);
  const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
  ed.focus({ preventScroll: true });
  const wr = range.getBoundingClientRect(), er = ed.getBoundingClientRect();
  // Instant rather than smooth: a smooth scroll here is cancelled by the selection/focus change in some browsers.
  ed.scrollTop = ed.scrollTop + (wr.top - er.top) - er.height / 2 + wr.height / 2;
  lastLocateKey = state.page + "|" + context.join(" ") + "|" + (i - lo);  // no need to re-locate what we just clicked
  if (state.mark) placeMarker(words[i]);
}
scroller.addEventListener("click", jumpToScanWord);

let locateTimer = null, lastLocateKey = "";
function locateCaret() {
  if (!state.doc || !state.page || (!state.follow && !state.mark)) { marker.hidden = true; edgeTicks.hidden = true; return; }
  if (selectedImg) return;  // a figure is selected: the dot stays on the figure
  clearTimeout(locateTimer);
  locateTimer = setTimeout(async () => {
    const ctx = caretContext();
    if (!ctx) return;
    const key = state.page + "|" + ctx.context.join(" ") + "|" + ctx.index;
    if (key === lastLocateKey) return;
    lastLocateKey = key;
    try {
      const r = await api(`/documents/${state.doc.doc_id}/pages/${state.page}/locate`, { method: "POST", body: ctx });
      if (!r.found) { marker.hidden = true; edgeTicks.hidden = true; return; }
      placeMarker(r.box);
    } catch (_) { /* ignore */ }
  }, 150);
}

function placeMarker(box) {
  // Dot sits just left of the word, vertically centred on it; positions are % of the image so zoom is irrelevant.
  state.lastBox = box;
  if (!img.clientHeight) return;  // image not laid out yet; the load handler re-places it
  const xPct = Math.max(0, box.x0 / 10 - 1.2), yPct = (box.y0 + box.y1) / 20;
  marker.style.left = xPct + "%"; marker.style.top = yPct + "%";
  marker.hidden = !state.mark;
  updateEdgeTicks();
  if (state.follow) {
    const wrap = $("#img-wrap");
    const y = wrap.offsetTop + (yPct / 100) * img.clientHeight;
    const x = wrap.offsetLeft + (xPct / 100) * img.clientWidth;
    const top = scroller.scrollTop, h = scroller.clientHeight;
    if (y < top + h * 0.2 || y > top + h * 0.8) scroller.scrollTo({ top: y - h / 2, behavior: "smooth" });
    const left = scroller.scrollLeft, w = scroller.clientWidth;
    if (img.clientWidth > w && (x < left + w * 0.1 || x > left + w * 0.9)) scroller.scrollTo({ left: x - w / 2, behavior: "smooth" });
  }
}
$("#editor").addEventListener("keyup", locateCaret);
$("#editor").addEventListener("mouseup", locateCaret);
$("#editor").addEventListener("focus", locateCaret);

// ------------------------------------------------------------------ resizable panels (drag the gutters)
function setupGutter(id, cssVar, measure, min) {
  const g = $(id);
  const saved = localStorage.getItem("remediate." + cssVar);
  if (saved) document.documentElement.style.setProperty(cssVar, saved);
  const apply = (px) => {
    const v = Math.max(min, Math.min(px, window.innerWidth - 300)) + "px";
    document.documentElement.style.setProperty(cssVar, v);
    localStorage.setItem("remediate." + cssVar, v);
  };
  g.addEventListener("mousedown", (e) => {
    e.preventDefault();
    const startX = e.clientX, startW = measure();
    document.body.classList.add("dragging"); g.classList.add("dragging");
    const move = (ev) => apply(startW + ev.clientX - startX);
    const up = () => { document.body.classList.remove("dragging"); g.classList.remove("dragging");
      window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up); };
    window.addEventListener("mousemove", move); window.addEventListener("mouseup", up);
  });
  g.addEventListener("keydown", (e) => {  // keyboard resize for accessibility
    if (e.key === "ArrowLeft") { apply(measure() - 20); e.preventDefault(); }
    if (e.key === "ArrowRight") { apply(measure() + 20); e.preventDefault(); }
  });
  g.addEventListener("dblclick", () => { localStorage.removeItem("remediate." + cssVar); document.documentElement.style.removeProperty(cssVar); });
}
setupGutter("#gutter-1", "--sidebar-w", () => $("#sidebar").getBoundingClientRect().width, 90);
setupGutter("#gutter-2", "--image-w", () => $("#pane-image").getBoundingClientRect().width, 120);

// ------------------------------------------------------------------ notes panel (toggle + warning when hidden but non-empty)
function updateNotesUI() {
  const visible = localStorage.getItem("remediate.notesVisible") !== "false";
  $("#notes-panel").hidden = !visible;
  $("#btn-notes").setAttribute("aria-pressed", String(visible));
  $("#notes-warn").hidden = visible || !$("#f-notes").value.trim();
}
$("#btn-notes").addEventListener("click", () => {
  const visible = localStorage.getItem("remediate.notesVisible") !== "false";
  localStorage.setItem("remediate.notesVisible", String(!visible));
  updateNotesUI();
  if (!visible) $("#f-notes").focus();
});
$("#f-notes").addEventListener("input", updateNotesUI);
updateNotesUI();

// ------------------------------------------------------------------ banner (messages from Claude / conflicts)
function showBanner(text, primary, secondary, tertiary) {
  const b = $("#banner");
  $("#banner-text").textContent = text;
  const p = $("#banner-primary"), s = $("#banner-secondary"), t = $("#banner-tertiary");
  p.hidden = !primary; s.hidden = !secondary; t.hidden = !tertiary;
  if (primary) { p.textContent = primary.label; p.onclick = () => { hideBanner(); primary.run(); }; }
  if (secondary) { s.textContent = secondary.label; s.onclick = () => { hideBanner(); secondary.run(); }; }
  if (tertiary) { t.textContent = tertiary.label; t.onclick = () => { hideBanner(); tertiary.run(); }; }
  b.hidden = false;
}
function hideBanner() { $("#banner").hidden = true; }
$("#banner-primary").addEventListener("click", () => {});

// ------------------------------------------------------------------ documents
async function loadDocs(selectId) {
  state.docs = await api("/documents");
  const sel = $("#doc-select");
  sel.innerHTML = '<option value="">— document —</option>' +
    state.docs.map((d) => `<option value="${d.doc_id}">${escapeHtml(d.title || d.doc_id)}</option>`).join("");
  if (selectId) { sel.value = selectId; await openDoc(selectId); }
}

async function openDoc(docId, pageToShow) {
  storeDraftNow();  // keep unsaved edits of the page we are leaving, whatever document it belongs to
  if (!docId) { state.doc = null; renderPages(); return; }
  state.doc = await api(`/documents/${docId}`);
  localStorage.setItem("remediate.lastDoc", docId);
  renderPages(); renderMeta();
  ["#btn-transcribe", "#btn-build", "#btn-validate"].forEach((b) => ($(b).disabled = false));
  $("#lnk-html").href = `/api/documents/${docId}/output/html`;
  $("#lnk-epub").href = `/api/documents/${docId}/output/epub`;
  if (state.doc.job) pollJob(state.doc.job.id);
  const first = pageToShow || (state.doc.pages.find((p) => p.status === "needs_review") || state.doc.pages[0] || {}).index;
  state.page = null; state.dirty = false;  // switching documents: the previous page's draft is already stored
  if (first) loadPage(first);
  startPolling();
}

const STATUS_LABEL = { done: "approved", needs_review: "needs review", pending: "not transcribed", error: "error" };
const statusLabel = (s) => STATUS_LABEL[s] || s;

function renderMeta() {
  const s = state.doc.status_counts || {};
  const el = $("#doc-meta");
  const drafts = new Set(Object.keys(docDrafts()).map(Number));
  if (state.dirty && state.page) drafts.add(state.page);
  const draftCount = drafts.size;
  el.textContent = `${s.done || 0}/${state.doc.page_count} approved · ${s.needs_review || 0} review${draftCount ? ` · ${draftCount} unsaved` : ""}`;
  el.title = `${state.doc.page_count} pages · ${s.done || 0} approved · ${s.needs_review || 0} need review · ${s.pending || 0} not transcribed · ${s.error || 0} errors · ${draftCount} with unsaved edits`;
  const all = $("#btn-save-all");
  all.disabled = !draftCount; all.textContent = draftCount ? `Save (${draftCount})` : "Save";
}

// ------------------------------------------------------------------ drafts: unsaved edits kept per page, in memory and localStorage
function allDrafts() {
  try { return JSON.parse(localStorage.getItem("remediate.drafts") || "{}"); } catch (_) { return {}; }
}
function writeDrafts(d) { try { localStorage.setItem("remediate.drafts", JSON.stringify(d)); } catch (_) { /* storage full or disabled */ } }
function docDrafts() { return (state.doc && allDrafts()[state.doc.doc_id]) || {}; }
function getDraft(n) { return docDrafts()[n] || null; }
function setDraft(n, draft) {
  const d = allDrafts(); (d[state.doc.doc_id] = d[state.doc.doc_id] || {})[n] = draft; writeDrafts(d);
}
function clearDraft(n, docId = state.doc.doc_id) {
  const d = allDrafts(); if (d[docId]) { delete d[docId][n]; if (!Object.keys(d[docId]).length) delete d[docId]; } writeDrafts(d);
}
/* Snapshot of the editor and page fields for the current page. */
function formSnapshot() {
  return { html: currentHtml(), label: $("#f-label").value.trim() || null,
    starts_mid_paragraph: $("#f-starts").checked, ends_mid_paragraph: $("#f-ends").checked,
    skip: $("#f-skip").checked, notes: $("#f-notes").value.trim(),
    figures: (state.pageData && state.pageData.figures) || [], version: state.pageData ? state.pageData.version : null,
    at: Date.now() };
}
let draftTimer = null;
function storeDraftSoon() { clearTimeout(draftTimer); draftTimer = setTimeout(storeDraftNow, 400); }
function storeDraftNow() {
  clearTimeout(draftTimer);
  if (!state.doc || !state.page || !state.dirty) return;
  setDraft(state.page, formSnapshot());
  renderPages(); renderMeta();
}
function applyDraft(d) {
  $("#editor").innerHTML = d.html || ""; showFigures($("#editor"));
  $("#source").value = d.html || "";
  $("#f-label").value = d.label || "";
  $("#f-starts").checked = !!d.starts_mid_paragraph; $("#f-ends").checked = !!d.ends_mid_paragraph;
  $("#f-skip").checked = !!d.skip; $("#f-notes").value = d.notes || "";
  if (d.figures) state.pageData.figures = d.figures;
  state.dirty = true;
}

function whoLabel(p) {
  if (!p.changed_by || p.changed_by === "editor") return "";
  return p.changed_by === "claude" ? "C" : "M";
}

function renderPages() {
  const list = $("#page-list");
  if (!state.doc) { list.innerHTML = ""; return; }
  const drafts = docDrafts();
  list.innerHTML = state.doc.pages.map((p) => {
    const who = whoLabel(p);
    const hasDraft = !!drafts[p.index] || (p.index === state.page && state.dirty);
    const title = `PDF page ${p.index}${p.label ? ", printed " + p.label : ""}: ${statusLabel(p.status)}${p.skip ? " (skipped)" : ""}` +
      (hasDraft ? " · unsaved edits" : "") + (p.changed_by ? ` · last changed by ${p.changed_by}` : "") + (p.notes ? ` · ${p.notes}` : "");
    return `<li data-page="${p.index}" aria-current="${p.index === state.page}" title="${escapeHtml(title)}">
      <span class="dot ${p.status}" aria-hidden="true"></span>
      <span class="num">${p.index}</span>
      ${hasDraft ? '<span class="draft-mark" title="unsaved edits">✎</span>' : ""}
      ${who ? `<span class="who" title="last changed by ${escapeHtml(p.changed_by)}">${who}</span>` : ""}
      <span class="lbl">${p.skip ? "skip" : p.label ? escapeHtml(p.label) : ""}</span>
      <span class="visually-hidden">${statusLabel(p.status)}${hasDraft ? ", unsaved edits" : ""}</span>
    </li>`;
  }).join("");
}

// ------------------------------------------------------------------ pages
async function loadPage(n, opts = {}) {
  // Leaving a page never loses anything: unsaved edits are kept as a draft and restored on return.
  if (state.dirty && !opts.discardCurrent && n !== state.page) storeDraftNow();
  else if (opts.discardCurrent) clearDraft(state.page);
  const d = await api(`/documents/${state.doc.doc_id}/pages/${n}`);
  state.page = n; state.pageData = d; state.dirty = false;
  hideBanner();
  const draft = opts.discardCurrent || opts.fresh ? null : getDraft(n);
  const ind = $("#page-indicator");
  ind.textContent = `${n}/${d.of}`;
  ind.title = `PDF page ${n} of ${d.of}${d.label ? ", printed page " + d.label : ""}`;
  img.hidden = false;
  img.src = `/api/documents/${state.doc.doc_id}/pages/${n}/image`;
  $("#editor").innerHTML = d.html || "";
  showFigures($("#editor"));
  $("#source").value = d.html || "";
  $("#draft").textContent = d.draft_text || "(no draft text)";
  $("#f-label").value = d.label || "";
  $("#f-starts").checked = !!d.starts_mid_paragraph;
  $("#f-ends").checked = !!d.ends_mid_paragraph;
  $("#f-skip").checked = !!d.skip;
  $("#f-notes").value = d.notes || "";
  if (draft) {
    applyDraft(draft);
    if (draft.version !== d.version) {
      // The page changed on the server (e.g. Claude edited it) after this draft was made: keep the
      // draft's version so a save is refused and the conflict banner offers reload or overwrite.
      state.pageData.version = draft.version;
      showBanner(`Page ${n} was changed by ${d.changed_by || "someone"} after your unsaved edits were made.`,
        { label: "Show their version", run: () => loadPage(n, { discardCurrent: true }) },
        { label: "Keep my edits", run: () => {} });
    }
  }
  updateNotesUI();
  ["#btn-approve", "#btn-approve-next"].forEach((b) => ($(b).disabled = false));
  renderPages(); renderMeta();
  if (!opts.silent) {
    const who = d.changed_by && d.changed_by !== "editor" ? ` · last changed by ${d.changed_by}` : "";
    setStatus(`Page ${n}: ${statusLabel(d.status)}${who}${d.notes ? " · has notes" : ""}${draft ? " · restored unsaved edits" : ""}`);
  }
  $("#editor").scrollTop = 0;
  marker.hidden = true; edgeTicks.hidden = true; lastLocateKey = ""; state.lastBox = null;
  selectFigure(null);
  reportView();
}

/* Figures are stored as <img src="fig:ID">; in the editor they are shown from the crop endpoint and
   restored to fig:ID when the HTML is read back. */
function showFigures(ed) {
  ed.querySelectorAll('img[src^="fig:"]').forEach((im) => {
    const id = im.getAttribute("src").slice(4);
    im.dataset.fig = id;
    im.src = `/api/documents/${state.doc.doc_id}/pages/${state.page}/figure/${encodeURIComponent(id)}`;
  });
}
function editorHtml() {
  const clone = $("#editor").cloneNode(true);
  clone.querySelectorAll("img[data-fig]").forEach((im) => { im.setAttribute("src", "fig:" + im.dataset.fig); delete im.dataset.fig; });
  clone.querySelectorAll('img[src^="data:"]').forEach((im) => im.removeAttribute("src"));  // placeholder of a figure with no crop yet
  clone.querySelectorAll("img.selected").forEach((im) => im.removeAttribute("class"));
  clone.querySelectorAll(".selected-figure").forEach((f) => { f.classList.remove("selected-figure"); if (!f.classList.length) f.removeAttribute("class"); });
  return clone.innerHTML;
}
function currentHtml() {
  return $("#toggle-source").checked ? $("#source").value : editorHtml();
}

/* Write one page to the document. approve=true marks it approved ("done"); otherwise its edits are
   saved as needing review. Returns the saved page or null (a 409 conflict is reported to the caller). */
async function persistPage(n, snap, { approve = false, overwrite = false } = {}) {
  const body = {
    html: snap.html, label: snap.label, starts_mid_paragraph: snap.starts_mid_paragraph,
    ends_mid_paragraph: snap.ends_mid_paragraph, skip: snap.skip, notes: snap.notes,
    status: approve ? "done" : "needs_review", version: overwrite ? null : snap.version,
    figures: snap.figures || [],
  };
  return api(`/documents/${state.doc.doc_id}/pages/${n}`, { method: "PUT", body });
}

async function savePage(andNext = false, overwrite = false, approve = false) {
  if (!state.doc || !state.page) return;
  const n = state.page;
  try {
    const saved = await persistPage(n, formSnapshot(), { approve, overwrite });
    state.dirty = false; clearDraft(n); state.pageData = { ...state.pageData, ...saved };
    $("#editor").innerHTML = saved.html; showFigures($("#editor")); $("#source").value = saved.html;
    const p = state.doc.pages.find((x) => x.index === n);
    Object.assign(p, saved);
    renderPages(); renderMeta();
    setStatus(`${approve ? "Approved" : "Saved"} page ${n} (${saved.words} words)`);
    reportView();
    if (andNext && n < state.doc.page_count) loadPage(n + 1);
  } catch (e) {
    if (e.status === 409) {
      showBanner(e.message,
        { label: "Reload their version", run: () => loadPage(n, { discardCurrent: true }) },
        { label: "Overwrite with mine", run: () => savePage(andNext, true, approve) });
    } else setStatus("Save failed: " + e.message, true);
  }
}

/* Save every page with unsaved edits (current page from the form, others from their drafts). */
async function saveAll() {
  if (!state.doc) return;
  let ok = 0, conflicts = [], failed = [];
  if (state.dirty) { await savePage(false); if (!state.dirty) ok++; else conflicts.push(state.page); }
  const drafts = docDrafts();
  for (const n of Object.keys(drafts).map(Number).sort((a, b) => a - b)) {
    if (n === state.page) continue;
    try {
      const saved = await persistPage(n, drafts[n]);
      clearDraft(n); ok++;
      const p = state.doc.pages.find((x) => x.index === n); if (p) Object.assign(p, saved);
    } catch (e) { (e.status === 409 ? conflicts : failed).push(n); }
  }
  renderPages(); renderMeta();
  let msg = `Saved ${ok} page${ok === 1 ? "" : "s"}.`;
  if (conflicts.length) msg += ` Pages ${conflicts.join(", ")} changed on the server since your edits; open them to resolve.`;
  if (failed.length) msg += ` Failed: ${failed.join(", ")}.`;
  setStatus(msg, conflicts.length + failed.length > 0);
}

function markDirty() { if (!state.dirty) { state.dirty = true; reportView(); renderPages(); renderMeta(); } storeDraftSoon(); }

// ------------------------------------------------------------------ collaboration: report view, poll, follow
let reportTimer = null;
function reportView() {
  if (!state.doc || !state.page) return;
  clearTimeout(reportTimer);
  reportTimer = setTimeout(() => {
    api("/session/view", { method: "PUT", body: {
      doc_id: state.doc.doc_id, page: state.page, label: $("#f-label").value.trim() || null,
      selection: state.lastSelection, dirty: state.dirty } }).catch(() => {});
  }, 300);
}

document.addEventListener("selectionchange", () => {
  const sel = window.getSelection();
  const ed = $("#editor");
  let text = "";
  if (sel && !sel.isCollapsed && ed.contains(sel.anchorNode)) text = sel.toString().slice(0, 2000);
  if (text !== state.lastSelection) { state.lastSelection = text; reportView(); }
});

function startPolling() {
  if (state.polling) return;
  state.polling = setInterval(poll, 2000);
}

async function poll() {
  if (!state.doc || document.hidden) return;
  try {
    const s = await api("/session");
    const req = s.requested;
    if (req && req.at > state.lastRequestAt) {
      state.lastRequestAt = req.at;
      await api("/session/requested", { method: "DELETE" });
      const go = async () => {
        if (req.doc_id !== state.doc.doc_id) { $("#doc-select").value = req.doc_id; await openDoc(req.doc_id, req.page); }
        else await loadPage(req.page);
        if (req.note) setStatus("Claude: " + req.note);
      };
      await go();  // unsaved edits on the current page are kept as a draft
    }
    const fresh = await api(`/documents/${state.doc.doc_id}`);
    state.doc.pages = fresh.pages; state.doc.status_counts = fresh.status_counts;
    renderPages(); renderMeta();
    const cur = fresh.pages.find((p) => p.index === state.page);
    if (cur && state.pageData && cur.version !== state.pageData.version && $("#banner").hidden) {
      const who = cur.changed_by || "someone";
      if (!state.dirty) {
        await loadPage(state.page, { fresh: true, silent: true });
        showBanner(`Page ${state.page} was updated by ${who}. Check it and Approve to confirm.`,
          { label: "OK", run: () => {} }, null);
      } else {
        showBanner(`Page ${state.page} was changed by ${who} while you were editing.`,
          { label: "Reload their version", run: () => loadPage(state.page, { discardCurrent: true }) },
          { label: "Keep my edits", run: () => { state.pageData.version = cur.version; storeDraftNow(); } });
      }
    }
  } catch (_) { /* server briefly unavailable; try again next tick */ }
}

// ------------------------------------------------------------------ editor commands
document.execCommand("defaultParagraphSeparator", false, "p");
document.querySelectorAll("[data-cmd]").forEach((b) => b.addEventListener("click", () => {
  $("#editor").focus(); document.execCommand(b.dataset.cmd, false, null); markDirty();
}));
$("#block-style").addEventListener("change", (e) => {
  $("#editor").focus(); document.execCommand("formatBlock", false, e.target.value); markDirty();
});

// Alignment is a class on the paragraph or heading (the sanitizer keeps only align-center / align-right
// there); left is the default, so it just removes the class.
const ALIGNABLE = "p, h1, h2, h3, h4, h5, h6";
function selectedAlignBlocks() {
  const sel = window.getSelection();
  const ed = $("#editor");
  if (!sel || !sel.rangeCount || !ed.contains(sel.anchorNode)) return [];
  const range = sel.getRangeAt(0);
  if (!sel.isCollapsed) {
    const hit = [...ed.querySelectorAll(ALIGNABLE)].filter((b) => range.intersectsNode(b));
    if (hit.length) return hit;
  }
  const node = range.startContainer;
  const block = (node.nodeType === 1 ? node : node.parentElement).closest(ALIGNABLE);
  return block && ed.contains(block) ? [block] : [];
}
function updateAlignButtons() {
  const blocks = selectedAlignBlocks();
  const current = (b) => ["align-center", "align-right"].find((c) => b.classList.contains(c)) || "";
  document.querySelectorAll("[data-align]").forEach((btn) => btn.setAttribute("aria-pressed",
    String(blocks.length > 0 && blocks.every((b) => current(b) === btn.dataset.align))));
}
document.querySelectorAll("[data-align]").forEach((btn) => {
  btn.addEventListener("mousedown", (e) => e.preventDefault());  // keep the editor selection
  btn.addEventListener("click", () => {
    const blocks = selectedAlignBlocks();
    if (!blocks.length) { setStatus("Put the caret in a paragraph or heading to align it."); return; }
    blocks.forEach((b) => {
      b.classList.remove("align-left", "align-center", "align-right");
      if (btn.dataset.align) b.classList.add(btn.dataset.align);
      if (!b.classList.length) b.removeAttribute("class");
    });
    updateAlignButtons(); markDirty();
  });
});
document.addEventListener("selectionchange", updateAlignButtons);

$("#editor").addEventListener("input", markDirty);
$("#source").addEventListener("input", markDirty);
["#f-label", "#f-starts", "#f-ends", "#f-skip", "#f-notes"].forEach((s) => $(s).addEventListener("change", markDirty));

// ------------------------------------------------------------------ figure panel (alt text, AI autofill, alignment, wrap)
let selectedImg = null;
/* The element carrying a figure's layout classes: its <figure>, or the <p> a browser wraps an inserted
   image in (the sanitizer turns such a paragraph into a <figure> on save). */
function figureWrapper(imgEl) {
  if (!imgEl) return null;
  const f = imgEl.closest("figure");
  if (f) return f;
  const par = imgEl.parentElement;  // browsers may add a <br> beside an inserted image
  return par && par.tagName === "P" && !par.textContent.trim() &&
    [...par.children].every((c) => c === imgEl || c.tagName === "BR") ? par : null;
}
function selectFigure(imgEl) {
  document.querySelectorAll("#editor img.selected").forEach((i) => i.classList.remove("selected"));
  document.querySelectorAll("#editor figure.selected-figure").forEach((f) => f.classList.remove("selected-figure"));
  selectedImg = imgEl;
  if (!imgEl) { $("#figure-panel").hidden = true; cropBox.hidden = true; return; }
  imgEl.classList.add("selected");
  const fig = figureWrapper(imgEl);
  if (fig) fig.classList.add("selected-figure");
  $("#figure-label").textContent = "Figure" + (imgEl.dataset.fig ? " " + imgEl.dataset.fig : "");
  $("#fig-alt").value = imgEl.getAttribute("alt") || "";
  const cls = fig ? fig.classList : { contains: () => false };
  $("#fig-align").value = ["align-left", "align-center", "align-right"].find((c) => cls.contains(c)) || "";
  $("#fig-wrap").checked = cls.contains("wrap");
  $("#fig-ai").disabled = !imgEl.dataset.fig;
  $("#fig-ai").title = imgEl.dataset.fig ? "Ask the configured model to write alt text from the cropped image"
    : "AI autofill needs a figure with a crop box on the scan";
  $("#figure-panel").hidden = false;
  // Move the dot to the figure's box on the scan and show the crop handles.
  const box = figureBox(imgEl.dataset.fig);
  if (box && (state.mark || state.follow)) { lastLocateKey = "figure:" + imgEl.dataset.fig; placeMarker(box); }
  showCropBox(box);
  $("#fig-crop-hint").textContent = box ? "Drag the box or its handles on the scan to adjust the crop." : "No crop box yet.";
}
function figureBox(fid) {
  const f = fid && state.pageData && (state.pageData.figures || []).find((x) => x.id === fid);
  return f && f.bbox && f.bbox.length === 4 ? { x0: f.bbox[0], y0: f.bbox[1], x1: f.bbox[2], y1: f.bbox[3] } : null;
}

// ------------------------------------------------------------------ crop box: drag/resize the selected figure's bounds on the scan
const cropBox = $("#crop-box");
function showCropBox(box) {
  if (!box) { cropBox.hidden = true; return; }
  cropBox.style.left = box.x0 / 10 + "%"; cropBox.style.top = box.y0 / 10 + "%";
  cropBox.style.width = (box.x1 - box.x0) / 10 + "%"; cropBox.style.height = (box.y1 - box.y0) / 10 + "%";
  cropBox.hidden = false;
}
/* Store a new box for the selected figure (creating the record if needed), refresh the editor preview. */
function commitCrop(box) {
  if (!selectedImg) return;
  let fid = selectedImg.dataset.fig;
  const figs = state.pageData.figures || (state.pageData.figures = []);
  if (!fid) {
    fid = newFigureId();
    selectedImg.dataset.fig = fid;
  }
  let f = figs.find((x) => x.id === fid);
  if (!f) { f = { id: fid, alt: selectedImg.getAttribute("alt") || "", bbox: null, caption: "" }; figs.push(f); }
  f.bbox = [box.x0, box.y0, box.x1, box.y1].map((v) => Math.round(v * 10) / 10);
  selectedImg.src = `/api/documents/${state.doc.doc_id}/pages/${state.page}/figure/${encodeURIComponent(fid)}?bbox=${f.bbox.join(",")}`;
  $("#figure-label").textContent = "Figure " + fid;
  $("#fig-ai").disabled = false;
  $("#fig-crop-hint").textContent = "Crop updated; Save to keep it.";
  showCropBox(box);
  if (state.mark || state.follow) { lastLocateKey = "figure:" + fid; placeMarker(box); }
  markDirty();
}
function newFigureId() {
  const label = (state.pageData && state.pageData.label) || ("page" + state.page);
  const used = new Set((state.pageData.figures || []).map((f) => f.id));
  $("#editor").querySelectorAll("img[data-fig]").forEach((im) => used.add(im.dataset.fig));
  let n = 1;
  while (used.has(`${label}-${n}`)) n++;
  return `${label}-${n}`;
}
function pagePoint(evt) {  // pointer position in 0-1000 page coordinates
  const r = img.getBoundingClientRect();
  return { x: Math.max(0, Math.min(1000, (evt.clientX - r.left) / r.width * 1000)),
           y: Math.max(0, Math.min(1000, (evt.clientY - r.top) / r.height * 1000)) };
}
let cropDrag = null;  // {mode, start, box} while dragging; suppresses the click-to-jump that follows a drag
cropBox.addEventListener("mousedown", (e) => {
  if (!selectedImg) return;
  e.preventDefault(); e.stopPropagation();
  const mode = e.target.dataset.h || "move";
  cropDrag = { mode, start: pagePoint(e), box: { ...figureBox(selectedImg.dataset.fig) }, moved: false };
  document.body.classList.add("dragging");
});
window.addEventListener("mousemove", (e) => {
  if (!cropDrag) return;
  const p = pagePoint(e), dx = p.x - cropDrag.start.x, dy = p.y - cropDrag.start.y;
  const b = { ...cropDrag.box }, m = cropDrag.mode, MIN = 8;
  // The three modes are exclusive: "draw" anchors the first click and stretches the opposite corner to
  // the cursor; "move" shifts the whole box; anything else is a resize handle (n/s/e/w/ne/...). Keep the
  // resize checks out of the draw branch: "draw" contains the letter w, which used to drag the left edge.
  if (m === "draw") {
    const s = cropDrag.start;
    b.x0 = Math.min(s.x, p.x); b.x1 = Math.max(s.x, p.x); b.y0 = Math.min(s.y, p.y); b.y1 = Math.max(s.y, p.y);
  } else if (m === "move") {
    const w = b.x1 - b.x0, h = b.y1 - b.y0;
    b.x0 = Math.max(0, Math.min(1000 - w, b.x0 + dx)); b.y0 = Math.max(0, Math.min(1000 - h, b.y0 + dy)); b.x1 = b.x0 + w; b.y1 = b.y0 + h;
  } else {
    if (m.includes("w")) b.x0 = Math.min(b.x0 + dx, b.x1 - MIN);
    if (m.includes("e")) b.x1 = Math.max(b.x1 + dx, b.x0 + MIN);
    if (m.includes("n")) b.y0 = Math.min(b.y0 + dy, b.y1 - MIN);
    if (m.includes("s")) b.y1 = Math.max(b.y1 + dy, b.y0 + MIN);
  }
  ["x0", "y0", "x1", "y1"].forEach((k) => (b[k] = Math.max(0, Math.min(1000, b[k]))));
  cropDrag.current = b; cropDrag.moved = true;
  showCropBox(b);
});
window.addEventListener("mouseup", () => {
  if (!cropDrag) return;
  const d = cropDrag; cropDrag = null;
  document.body.classList.remove("dragging"); scroller.classList.remove("drawing"); state.drawCrop = false;
  if (d.moved && d.current && d.current.x1 - d.current.x0 >= 5 && d.current.y1 - d.current.y0 >= 5) commitCrop(d.current);
  else if (d.mode === "draw") showCropBox(figureBox(selectedImg && selectedImg.dataset.fig));
  state.suppressClick = d.moved;  // the click event that follows a drag must not jump to a word
});
/* Draw mode: the next drag on the scan defines the selected figure's box. */
function armDrawCrop() {
  if (!selectedImg) { setStatus("Select a figure in the editor first.", true); return; }
  state.drawCrop = true; scroller.classList.add("drawing");
  setStatus("Drag over the scan to draw the figure's crop box.");
}
$("#fig-draw").addEventListener("click", armDrawCrop);
scroller.addEventListener("mousedown", (e) => {
  if (!state.drawCrop || e.target !== img) return;
  e.preventDefault();
  cropDrag = { mode: "draw", start: pagePoint(e), box: null, moved: false };
  document.body.classList.add("dragging");
});
/* Insert a new empty figure at the caret and arm draw mode for its crop box. */
const PLACEHOLDER_SRC = "data:image/svg+xml," + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60"><rect width="120" height="60" fill="#ddd"/><text x="60" y="35" font-size="12" text-anchor="middle" fill="#555">draw crop</text></svg>');
$("#btn-insert-figure").addEventListener("click", () => {
  const ed = $("#editor"); ed.focus();
  const fid = newFigureId();
  // Everything goes through execCommand so the browser's undo stack records it: move the caret to the
  // end of the current block, open a new paragraph, and insert the image there. Ctrl+Z removes the
  // image, a second Ctrl+Z the empty paragraph (an empty paragraph is dropped on save anyway).
  // Inserting a <figure> directly is not an option: browsers rewrap block HTML on insertion.
  const sel = window.getSelection();
  const range = document.createRange();
  let block = null;
  if (sel && sel.rangeCount && ed.contains(sel.anchorNode)) {
    block = sel.anchorNode.nodeType === Node.TEXT_NODE ? sel.anchorNode.parentElement : sel.anchorNode;
    block = block.closest(BLOCK_SEL);
    if (block === ed || !ed.contains(block)) block = null;
  }
  range.selectNodeContents(block || ed); range.collapse(false);
  sel.removeAllRanges(); sel.addRange(range);
  const html = `<img data-fig="${escapeHtml(fid)}" alt="" src="${PLACEHOLDER_SRC}">`;
  if (block) document.execCommand("insertParagraph");
  if (!document.execCommand("insertHTML", false, html)) {  // fallback for browsers without insertHTML
    const t = document.createElement("template"); t.innerHTML = `<figure>${html}</figure>`; range.insertNode(t.content.firstChild);
  }
  const im = ed.querySelector(`img[data-fig="${CSS.escape(fid)}"]`);
  markDirty(); if (im) { selectFigure(im); armDrawCrop(); }
});
// An undo (or any edit) that removes the selected figure closes its panel and crop box.
$("#editor").addEventListener("input", () => { if (selectedImg && !$("#editor").contains(selectedImg)) selectFigure(null); });
$("#editor").addEventListener("click", (e) => {
  const imgEl = e.target.tagName === "IMG" ? e.target : e.target.closest && e.target.closest("figure") ? e.target.closest("figure").querySelector("img") : null;
  selectFigure(imgEl || null);
});
$("#btn-alt").addEventListener("click", () => {
  if (!selectedImg) { setStatus("Click an image in the editor first.", true); return; }
  selectFigure(selectedImg); $("#fig-alt").focus();
});
$("#fig-close").addEventListener("click", () => selectFigure(null));
$("#fig-alt").addEventListener("input", () => { if (selectedImg) { selectedImg.setAttribute("alt", $("#fig-alt").value); markDirty(); } });
function applyFigureLayout() {
  const fig = figureWrapper(selectedImg);
  if (!fig) return;
  ["align-left", "align-center", "align-right", "wrap"].forEach((c) => fig.classList.remove(c));
  const align = $("#fig-align").value;
  if (align) fig.classList.add(align);
  if ($("#fig-wrap").checked && (align === "align-left" || align === "align-right")) fig.classList.add("wrap");
  if ($("#fig-wrap").checked && !(align === "align-left" || align === "align-right")) setStatus("Wrap needs left or right alignment.");
  if (!fig.classList.length) fig.removeAttribute("class");
  markDirty();
}
$("#fig-align").addEventListener("change", applyFigureLayout);
$("#fig-wrap").addEventListener("change", applyFigureLayout);
$("#fig-ai").addEventListener("click", async () => {
  if (!selectedImg || !selectedImg.dataset.fig) return;
  const fig = figureWrapper(selectedImg);
  const caption = fig && fig.querySelector("figcaption") ? fig.querySelector("figcaption").textContent.trim() : "";
  // A little context: the text of the blocks before and after the figure.
  const around = [];
  if (fig) { if (fig.previousElementSibling) around.push(fig.previousElementSibling.textContent); if (fig.nextElementSibling) around.push(fig.nextElementSibling.textContent); }
  $("#fig-ai").disabled = true; setStatus("Asking the model for alt text…");
  try {
    const r = await api(`/documents/${state.doc.doc_id}/pages/${state.page}/figures/${encodeURIComponent(selectedImg.dataset.fig)}/describe`,
      { method: "POST", body: { caption, context: around.join("\n").slice(0, 1500) } });
    if (r.decorative) setStatus(`${r.model} judged this image decorative; alt text left empty. Override if it carries meaning.`);
    else setStatus(`Alt text suggested by ${r.model}; edit as needed and Save.`);
    $("#fig-alt").value = r.alt; selectedImg.setAttribute("alt", r.alt); markDirty();
  } catch (e) { setStatus("AI autofill failed: " + e.message, true); }
  finally { $("#fig-ai").disabled = false; }
});
$("#btn-lang").addEventListener("click", () => {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed) { setStatus("Select the foreign-language text first.", true); return; }
  const lang = prompt("Language code for the selection (e.g. fr, de, la):", "");
  if (!lang) return;
  const span = document.createElement("span"); span.lang = lang.trim();
  const range = sel.getRangeAt(0); span.appendChild(range.extractContents()); range.insertNode(span);
  markDirty();
});
$("#toggle-source").addEventListener("change", (e) => {
  const src = e.target.checked;
  if (src) { $("#source").value = editorHtml(); } else { $("#editor").innerHTML = $("#source").value; showFigures($("#editor")); }
  $("#source").hidden = !src; $("#editor").hidden = src;
  if (src) { $("#toggle-draft").checked = false; $("#draft").hidden = true; }
});
$("#toggle-draft").addEventListener("change", (e) => {
  $("#draft").hidden = !e.target.checked;
  if (e.target.checked) { $("#editor").hidden = true; $("#source").hidden = true; $("#toggle-source").checked = false; }
  else { $("#editor").hidden = false; }
});
$("#btn-approve").addEventListener("click", () => savePage(false, false, true));
$("#btn-approve-next").addEventListener("click", () => savePage(true, false, true));
$("#btn-save-all").addEventListener("click", saveAll);
$("#btn-prev").addEventListener("click", () => state.page > 1 && loadPage(state.page - 1));
$("#btn-next").addEventListener("click", () => state.doc && state.page < state.doc.page_count && loadPage(state.page + 1));
$("#page-list").addEventListener("click", (e) => { const li = e.target.closest("li[data-page]"); if (li) loadPage(+li.dataset.page); });
$("#doc-select").addEventListener("change", (e) => openDoc(e.target.value));
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); saveAll(); }
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); savePage(false, false, true); }
  if (e.altKey && e.key === "ArrowLeft") { e.preventDefault(); $("#btn-prev").click(); }
  if (e.altKey && e.key === "ArrowRight") { e.preventDefault(); $("#btn-next").click(); }
});
window.addEventListener("beforeunload", () => storeDraftNow());  // drafts survive a reload

// ------------------------------------------------------------------ dialogs
function wireDialog(id) {
  const dlg = $(id);
  dlg.querySelector(".dlg-cancel").addEventListener("click", () => dlg.close("cancel"));
  return dlg;
}
const dlgOpen = wireDialog("#dlg-open"), dlgTranscribe = wireDialog("#dlg-transcribe"), dlgSettings = wireDialog("#dlg-settings");

$("#btn-open").addEventListener("click", () => dlgOpen.showModal());
$("#form-open").addEventListener("submit", async (e) => {
  const f = new FormData(e.target);
  try {
    let doc;
    if (f.get("file") && f.get("file").size > 0) {
      const fd = new FormData(); fd.append("file", f.get("file")); ["title", "author", "language"].forEach((k) => fd.append(k, f.get(k)));
      doc = await api("/documents/upload", { method: "POST", body: fd });
    } else {
      doc = await api("/documents", { method: "POST", body: { pdf_path: f.get("pdf_path"), title: f.get("title"), author: f.get("author"), language: f.get("language") || "en" } });
    }
    await loadDocs(doc.doc_id);
    setStatus(`Opened ${doc.title} (${doc.page_count} pages)`);
  } catch (err) { setStatus("Open failed: " + err.message, true); }
});

$("#btn-transcribe").addEventListener("click", async () => {
  state.settings = await api("/settings");
  const s = state.settings;
  const fb = s.fallback_backend && s.fallback_backend !== "none" && s.fallback_backend !== s.backend
    ? ` Refused pages are retried on ${s.fallback_backend === "anthropic" ? "Claude API, model " + s.model : "the local endpoint, model " + s.openai_model}.` : "";
  $("#transcribe-backend").textContent = (s.backend === "anthropic"
    ? `Using Claude API, model ${s.model}, effort ${s.effort}.` : `Using local endpoint ${s.openai_base_url}, model ${s.openai_model}.`) + fb;
  dlgTranscribe.showModal();
});
$("#form-transcribe").addEventListener("submit", async (e) => {
  const f = new FormData(e.target);
  try {
    const job = await api(`/documents/${state.doc.doc_id}/transcribe`, { method: "POST",
      body: { pages: f.get("pages") || "all", force: !!f.get("force"), instructions: f.get("instructions") || "" } });
    pollJob(job.id);
  } catch (err) { setStatus("Could not start: " + err.message, true); }
});

async function pollJob(jobId) {
  const job = await api(`/jobs/${jobId}`);
  state.job = job;
  if (job.status === "running") {
    setStatus(`Transcribing with ${job.model}: ${job.completed}/${job.requested} pages${job.errors ? ", " + job.errors + " errors" : ""} — ${job.last}`);
    setTimeout(() => pollJob(jobId), 2000);
  } else {
    const fresh = await api(`/documents/${state.doc.doc_id}`);
    state.doc = fresh; renderPages(); renderMeta();
    if (job.status === "error") setStatus("Transcription failed: " + job.error, true);
    else {
      const u = job.summary.usage || {};
      const refused = job.summary.refused
        ? ` · ${job.summary.refused} refused by ${job.summary.model}${job.summary.fell_back ? `, ${job.summary.fell_back} redone by ${job.summary.fallback_model}` : ""}` : "";
      setStatus(`Transcribed ${job.summary.done} pages (${job.summary.errors} errors)${refused} · ${u.input_tokens || 0} in / ${u.output_tokens || 0} out tokens`);
    }
    if (state.page && !state.dirty) loadPage(state.page, { fresh: true });
  }
}

$("#btn-build").addEventListener("click", async () => {
  if (state.dirty) await savePage(false);
  if (Object.keys(docDrafts()).length) await saveAll();
  setStatus("Building…");
  try {
    const r = await api(`/documents/${state.doc.doc_id}/build`, { method: "POST" });
    $("#lnk-html").hidden = false; $("#lnk-epub").hidden = !r.epub;
    showResults("Build", `<p>Wrote <code>${escapeHtml(r.html)}</code>${r.epub ? " and <code>" + escapeHtml(r.epub) + "</code>" : ""}. ` +
      `<a href="/api/documents/${state.doc.doc_id}/preview" target="_blank" rel="noopener">Open HTML preview</a></p>` +
      (r.warnings.length ? "<ul>" + r.warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("") + "</ul>" : "<p>No warnings.</p>"));
    setStatus("Build finished");
  } catch (e) { setStatus("Build failed: " + e.message, true); }
});

$("#btn-validate").addEventListener("click", async () => {
  setStatus("Validating (epubcheck may take a moment)…");
  try {
    const r = await api(`/documents/${state.doc.doc_id}/validate`);
    const c = r.counts;
    const rows = r.issues.map((i) => {
      const m = /page (\d+)/.exec(i.location || "");
      const loc = m ? `<a href="#" data-goto="${m[1]}">${escapeHtml(i.location)}</a>` : escapeHtml(i.location || "");
      return `<tr><td class="sev-${i.severity}">${i.severity}</td><td>${escapeHtml(i.code)}</td><td>${escapeHtml(i.message)}</td><td>${loc}</td></tr>`;
    }).join("");
    showResults(`Validation: ${c.error || 0} errors, ${c.warning || 0} warnings, ${c.info || 0} notes`,
      rows ? `<table>${rows}</table>` : "<p>No issues found.</p>");
    setStatus("Validation finished");
  } catch (e) { setStatus("Validation failed: " + e.message, true); }
});
$("#results-body").addEventListener("click", (e) => {
  const a = e.target.closest("a[data-goto]"); if (a) { e.preventDefault(); loadPage(+a.dataset.goto); }
});

function showResults(title, html) {
  $("#results").hidden = false; $("#results-title").textContent = title; $("#results-body").innerHTML = html;
}
$("#btn-results-close").addEventListener("click", () => ($("#results").hidden = true));

$("#btn-settings").addEventListener("click", async () => {
  const s = await api("/settings");
  const f = $("#form-settings");
  Object.entries(s).forEach(([k, v]) => {
    const el = f.elements[k];
    if (!el) return;
    if (el.type === "checkbox") el.checked = !!v; else el.value = v;
  });
  dlgSettings.showModal();
});
$("#form-settings").addEventListener("submit", async (e) => {
  const f = new FormData(e.target); const body = {};
  f.forEach((v, k) => (body[k] = k === "workers" ? +v : v));
  // An unticked checkbox is absent from FormData; send it explicitly.
  body.send_title = e.target.elements.send_title.checked;
  body.openai_disable_thinking = e.target.elements.openai_disable_thinking.checked;
  try { await api("/settings", { method: "PUT", body }); setStatus("Settings saved"); }
  catch (err) { setStatus("Settings not saved: " + err.message, true); }
});

function escapeHtml(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

// ------------------------------------------------------------------ start
loadDocs(localStorage.getItem("remediate.lastDoc") || "").catch((e) => setStatus(e.message, true));
