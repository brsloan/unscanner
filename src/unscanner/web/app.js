/* unscanner UI: plain JS, no build step. Talks to the JSON API in webapp.py.
   Collaboration: the UI reports what it shows to /api/session/view, polls the document every 2 s
   so edits made by Claude (through the MCP server) appear, follows show_page requests, and refuses
   to overwrite a page that changed underneath it (HTTP 409). */
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = { docs: [], doc: null, page: null, pageData: null, dirty: false, job: null, settings: null,
  lastRequestAt: 0, lastSelection: "", polling: null, busy: 0, gen: 0 };
/* busy counts page loads and saves in flight; gen changes when one starts or ends. poll() drops a
   document snapshot that overlapped either: a snapshot fetched before a save landed still has the
   old version and would look like someone else changed the page. */
async function tracked(promise) {
  state.busy++; state.gen++;
  try { return await promise; } finally { state.busy--; state.gen++; }
}

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

// ------------------------------------------------------------------ settings kept by the browser
// The program used to be called "remediate": move what it stored (unsaved drafts, preferences) to the new keys.
try {
  for (const key of Object.keys(localStorage)) {
    if (!key.startsWith("remediate.")) continue;
    const renamed = "unscanner." + key.slice("remediate.".length);
    if (localStorage.getItem(renamed) === null) localStorage.setItem(renamed, localStorage.getItem(key));
    localStorage.removeItem(key);
  }
} catch (_) { /* storage disabled */ }

// ------------------------------------------------------------------ page image scaling (fit / fill / manual)
const img = $("#page-image"), scroller = $("#image-scroll");
state.zoomMode = localStorage.getItem("unscanner.zoomMode") || "fit";
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
  state.zoomMode = mode; localStorage.setItem("unscanner.zoomMode", mode);
  if (mode !== "manual") $("#zoom").value = 100;
  applyZoom();
}
img.addEventListener("load", () => { applyZoom(); if (state.lastBox && (state.follow || state.mark)) placeMarker(state.lastBox); });
new ResizeObserver(applyZoom).observe(scroller);
$("#btn-fit").addEventListener("click", () => setZoomMode("fit"));
$("#btn-fill").addEventListener("click", () => setZoomMode("fill"));
$("#zoom").addEventListener("input", () => { state.zoomMode = "manual"; localStorage.setItem("unscanner.zoomMode", "manual"); applyZoom(); });

// ------------------------------------------------------------------ follow / mark: locate the editor caret on the scan
const marker = $("#marker"), edgeTicks = $("#edge-ticks");
state.follow = localStorage.getItem("unscanner.follow") === "true";
state.mark = localStorage.getItem("unscanner.mark") === "true";
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
$("#btn-follow").addEventListener("click", () => { state.follow = !state.follow; localStorage.setItem("unscanner.follow", state.follow); renderFollowButtons(); locateCaret(); });
$("#btn-mark").addEventListener("click", () => { state.mark = !state.mark; localStorage.setItem("unscanner.mark", state.mark); renderFollowButtons(); locateCaret(); });
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
    const left = scroller.scrollLeft, w = scroller.clientWidth;
    // One scrollTo for both axes: a second smooth scroll would cancel the first.
    const to = { behavior: "smooth" };
    if (y < top + h * 0.2 || y > top + h * 0.8) to.top = y - h / 2;
    if (img.clientWidth > w && (x < left + w * 0.1 || x > left + w * 0.9)) to.left = x - w / 2;
    if ("top" in to || "left" in to) scroller.scrollTo(to);
  }
}
$("#editor").addEventListener("keyup", locateCaret);
$("#editor").addEventListener("mouseup", locateCaret);
$("#editor").addEventListener("focus", locateCaret);

// ------------------------------------------------------------------ word diff: the transcription against the words on the scan
/* The server diffs the editor's words against the scan's word boxes (diff.py). Words the scan has and
   the transcription lacks are boxed on the scan; words the transcription has and the scan lacks are
   highlighted in the editor with the CSS Custom Highlight API, which leaves the edited HTML alone. */
const diffLayer = $("#diff-layer");
const canHighlight = !!(window.Highlight && window.CSS && CSS.highlights);
const DIFF_TITLE = $("#btn-diff").title;
state.diff = localStorage.getItem("unscanner.diff") !== "false";
let diffTimer = null, diffSeq = 0;
function clearDiff() {
  diffSeq++;  // an answer still on its way is dropped
  diffLayer.replaceChildren();
  if (canHighlight) { CSS.highlights.delete("diff-extra"); CSS.highlights.delete("diff-changed"); }
  $("#diff-count").hidden = true;
  $("#btn-diff").title = DIFF_TITLE;
}
function refreshDiffSoon(delay = 500) { clearTimeout(diffTimer); diffTimer = setTimeout(refreshDiff, delay); }
async function refreshDiff() {
  clearTimeout(diffTimer);
  $("#btn-diff").setAttribute("aria-pressed", String(state.diff));
  if (!state.diff || !state.doc || !state.page || $("#f-skip").checked) { clearDiff(); return; }
  // In Source view the editor is stale: diff the source text instead, for the scan side only.
  const live = !$("#toggle-source").checked;
  const ed = live ? $("#editor") : new DOMParser().parseFromString($("#source").value, "text/html").body;
  const { words, point } = editorWords(ed);
  // A figure description is written by the transcriber, never printed on the page.
  const shown = words.filter((w) => { const at = point(w.s); return !(at && at[0].parentElement.closest(".figure-description")); });
  if (!shown.length) { clearDiff(); return; }  // nothing transcribed yet: the whole scan would light up
  const seq = ++diffSeq, page = state.page;
  let r;
  try { r = await api(`/documents/${state.doc.doc_id}/pages/${page}/diff`, { method: "POST", body: { words: shown.map((w) => w.t) } }); }
  catch (_) { return; }
  if (seq !== diffSeq || page !== state.page) return;
  diffLayer.replaceChildren(...r.scan.flatMap((t) => t.boxes.map((b) => {
    const el = document.createElement("i");
    el.className = t.kind;
    el.style.cssText = `left:${b.x0 / 10}%;top:${b.y0 / 10}%;width:${(b.x1 - b.x0) / 10}%;height:${(b.y1 - b.y0) / 10}%`;
    return el;
  })));
  if (canHighlight) {
    const hl = { extra: new Highlight(), changed: new Highlight() };
    if (live) for (const e of r.editor) {
      const w = shown[e.index], a = w && point(w.s), b = w && point(w.e);
      if (!a || !b) continue;
      try { const range = document.createRange(); range.setStart(a[0], a[1]); range.setEnd(b[0], b[1]); hl[e.kind].add(range); }
      catch (_) { /* the text changed while the diff was being made; the next one is already due */ }
    }
    CSS.highlights.set("diff-extra", hl.extra); CSS.highlights.set("diff-changed", hl.changed);
  }
  const c = r.counts, total = c.missing + c.extra + c.changed;
  $("#diff-count").textContent = total; $("#diff-count").hidden = !total;
  $("#btn-diff").title = `${DIFF_TITLE}. This page: ${c.missing} on the scan but not transcribed, ${c.extra} transcribed but not on the scan, ${c.changed} spelled differently` +
    (canHighlight ? "" : ". This browser cannot highlight text in the editor; differences are shown on the scan only");
}
$("#btn-diff").addEventListener("click", () => { state.diff = !state.diff; localStorage.setItem("unscanner.diff", state.diff); refreshDiff(); });
$("#btn-diff").setAttribute("aria-pressed", String(state.diff));

// ------------------------------------------------------------------ resizable panels (drag the gutters)
function setupGutter(id, cssVar, measure, min) {
  const g = $(id);
  const saved = localStorage.getItem("unscanner." + cssVar);
  if (saved) document.documentElement.style.setProperty(cssVar, saved);
  const apply = (px) => {
    const v = Math.max(min, Math.min(px, window.innerWidth - 300)) + "px";
    document.documentElement.style.setProperty(cssVar, v);
    localStorage.setItem("unscanner." + cssVar, v);
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
  g.addEventListener("dblclick", () => { localStorage.removeItem("unscanner." + cssVar); document.documentElement.style.removeProperty(cssVar); });
}
setupGutter("#gutter-1", "--sidebar-w", () => $("#sidebar").getBoundingClientRect().width, 90);
setupGutter("#gutter-2", "--image-w", () => $("#pane-image").getBoundingClientRect().width, 120);

// ------------------------------------------------------------------ notes panel (toggle + warning when hidden but non-empty)
function updateNotesUI() {
  const visible = localStorage.getItem("unscanner.notesVisible") !== "false";
  $("#notes-panel").hidden = !visible;
  $("#btn-notes").setAttribute("aria-pressed", String(visible));
  $("#notes-warn").hidden = visible || !$("#f-notes").value.trim();
}
$("#btn-notes").addEventListener("click", () => {
  const visible = localStorage.getItem("unscanner.notesVisible") !== "false";
  localStorage.setItem("unscanner.notesVisible", String(!visible));
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
  if ($("#dlg-properties").open) $("#dlg-properties").close("cancel");  // it belongs to the document we are leaving
  if (!docId) { state.doc = null; renderPages(); return; }
  state.doc = await api(`/documents/${docId}`);
  localStorage.setItem("unscanner.lastDoc", docId);
  renderPages(); renderMeta();
  ["#btn-transcribe", "#btn-build", "#btn-validate", "#btn-properties", "#btn-export"].forEach((b) => ($(b).disabled = false));
  $("#lnk-html").href = `/api/documents/${docId}/output/html`;
  $("#lnk-epub").href = `/api/documents/${docId}/output/epub`;
  if (state.doc.job) pollJob(state.doc.job.id);
  const last = lastPages()[docId];
  const resume = state.doc.pages.some((p) => p.index === last) ? last : null;
  const first = pageToShow || resume || (state.doc.pages.find((p) => p.status === "needs_review") || state.doc.pages[0] || {}).index;
  state.page = null; state.dirty = false;  // switching documents: the previous page's draft is already stored
  if (first) loadPage(first);
  startPolling();
}

// The page last shown in each document, so reopening the app (or the document) resumes there.
function lastPages() {
  try { return JSON.parse(localStorage.getItem("unscanner.lastPage") || "{}"); } catch (_) { return {}; }
}
function rememberPage(docId, n) {
  try { localStorage.setItem("unscanner.lastPage", JSON.stringify({ ...lastPages(), [docId]: n })); } catch (_) { /* storage disabled */ }
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
  try { return JSON.parse(localStorage.getItem("unscanner.drafts") || "{}"); } catch (_) { return {}; }
}
function writeDrafts(d) { try { localStorage.setItem("unscanner.drafts", JSON.stringify(d)); } catch (_) { /* storage full or disabled */ } }
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
  if (d.figures) state.pageData.figures = d.figures;  // before showFigures, which reads the crop boxes
  $("#editor").innerHTML = d.html || ""; showFigures($("#editor"));
  setSource(d.html);
  $("#f-label").value = d.label || "";
  $("#f-starts").checked = !!d.starts_mid_paragraph; $("#f-ends").checked = !!d.ends_mid_paragraph;
  $("#f-skip").checked = !!d.skip; $("#f-notes").value = d.notes || "";
  state.dirty = true;
}

function whoLabel(p) {
  if (!p.changed_by || p.changed_by === "editor") return "";
  return p.changed_by === "claude" ? "C" : "M";
}

/* "needs work only": the page list, Approve & next and the page arrows use only pages not yet
   approved (needs review, not transcribed, error). The current page stays listed so approving it
   does not pull it out from under you. */
const reviewFilterOn = () => $("#filter-review").checked;
const needsWork = (p) => p.status !== "done";
function listedPages() {
  const pages = state.doc ? state.doc.pages : [];
  return reviewFilterOn() ? pages.filter((p) => needsWork(p) || p.index === state.page) : pages;
}
/* The next (step 1) or previous (step -1) page to go to from page n, or null. */
function neighbourPage(n, step) {
  if (!reviewFilterOn()) { const m = n + step; return m >= 1 && m <= state.doc.page_count ? m : null; }
  const todo = state.doc.pages.filter((p) => needsWork(p) && p.index !== n).map((p) => p.index);
  return (step > 0 ? todo.find((i) => i > n) : todo.reverse().find((i) => i < n)) ?? null;
}

function renderPages() {
  const list = $("#page-list");
  if (!state.doc) { list.innerHTML = ""; return; }
  const drafts = docDrafts();
  list.innerHTML = listedPages().map((p) => {
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
  const d = await tracked(api(`/documents/${state.doc.doc_id}/pages/${n}`));
  state.page = n; state.pageData = d; state.dirty = false;
  rememberPage(state.doc.doc_id, n);
  hideBanner();
  const draft = opts.discardCurrent || opts.fresh ? null : getDraft(n);
  const ind = $("#page-indicator");
  ind.textContent = `${n}/${d.of}`;
  ind.title = `PDF page ${n} of ${d.of}${d.label ? ", printed page " + d.label : ""}`;
  img.hidden = false;
  img.src = `/api/documents/${state.doc.doc_id}/pages/${n}/image`;
  clearDiff();
  $("#editor").innerHTML = d.html || "";
  showFigures($("#editor"));
  setSource(d.html);
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
  // Keep the current page visible in the list (on app open and when paging with the buttons).
  $(`#page-list li[data-page="${n}"]`)?.scrollIntoView({ block: "nearest" });
  if (!opts.silent) {
    const who = d.changed_by && d.changed_by !== "editor" ? ` · last changed by ${d.changed_by}` : "";
    setStatus(`Page ${n}: ${statusLabel(d.status)}${who}${d.notes ? " · has notes" : ""}${draft ? " · restored unsaved edits" : ""}`);
  }
  $("#editor").scrollTop = 0;
  marker.hidden = true; edgeTicks.hidden = true; lastLocateKey = ""; state.lastBox = null;
  selectFigure(null);
  reportView();
  refreshDiffSoon(0);
}

/* Figures are stored as <img src="fig:ID">; in the editor they are shown from the crop endpoint and
   restored to fig:ID when the HTML is read back. The URL carries the crop box and rotation: the browser
   reuses an image it already loaded from the same URL, so a bare URL showed a figure's old orientation
   after a save or a page change. */
function showFigures(ed) {
  ed.querySelectorAll('img[src^="fig:"]').forEach((im) => {
    const id = im.getAttribute("src").slice(4);
    im.dataset.fig = id;
    const f = state.pageData && (state.pageData.figures || []).find((x) => x.id === id);
    im.src = f && f.bbox ? figurePreviewSrc(f)
      : `/api/documents/${state.doc.doc_id}/pages/${state.page}/figure/${encodeURIComponent(id)}`;
  });
}
/* Large figures (full-page plates) are scaled down to fit the editor; label those so it is clear the
   preview is smaller than the image that goes into the output. The label is display only. */
function markScaledFigures() {
  $("#editor").querySelectorAll("img[data-fig]").forEach((im) => {
    const holder = figureWrapper(im);
    if (!holder || !im.naturalWidth || !im.clientWidth) return;
    const pct = Math.round((im.clientWidth / im.naturalWidth) * 100);
    if (pct < 95) holder.dataset.preview = `Scaled preview: shown at ${pct}% of full size`;
    else delete holder.dataset.preview;
  });
}
$("#editor").addEventListener("load", (e) => { if (e.target.tagName === "IMG") markScaledFigures(); }, true);
window.addEventListener("resize", markScaledFigures);
function editorHtml() {
  const clone = $("#editor").cloneNode(true);
  clone.querySelectorAll("[data-preview]").forEach((el) => delete el.dataset.preview);
  clone.querySelectorAll("img[data-fig]").forEach((im) => { im.setAttribute("src", "fig:" + im.dataset.fig); delete im.dataset.fig; });
  clone.querySelectorAll('img[src^="data:"]').forEach((im) => im.removeAttribute("src"));  // placeholder of a figure with no crop yet
  clone.querySelectorAll("img.selected").forEach((im) => im.removeAttribute("class"));
  clone.querySelectorAll(".selected-figure").forEach((f) => { f.classList.remove("selected-figure"); if (!f.classList.length) f.removeAttribute("class"); });
  return clone.innerHTML;
}
/* The Source view shows the page HTML indented, one block per line. Only whitespace next to block
   elements is added, which the server drops again on save (sanitize.py LINE_TAGS; keep in step),
   so the stored HTML is unchanged. Text inside a paragraph, and all of a <pre>, is left alone. */
const LINE_TAGS = new Set(["H1", "H2", "H3", "H4", "H5", "H6", "P", "UL", "OL", "LI", "BLOCKQUOTE", "TABLE", "FIGURE",
  "DL", "PRE", "HR", "ASIDE", "SECTION", "DIV", "CAPTION", "THEAD", "TBODY", "TFOOT", "TR", "TH", "TD", "FIGCAPTION", "DT", "DD"]);
function prettyHtml(html) {
  const t = document.createElement("template");
  t.innerHTML = html || "";
  const isLine = (n) => n.nodeType === 1 && LINE_TAGS.has(n.tagName);
  const lines = [];
  const walk = (parent, indent) => {
    const holder = document.createElement("div");  // collects a run of text and inline elements
    const flush = () => {
      const s = holder.innerHTML.trim();
      if (s) lines.push(indent + s);
      holder.textContent = "";
    };
    parent.childNodes.forEach((n) => {
      if (!isLine(n)) { holder.appendChild(n.cloneNode(true)); return; }
      flush();
      if (n.tagName !== "PRE" && [...n.children].some(isLine)) {
        const [open, close] = n.cloneNode(false).outerHTML.split(/(?=<\/[^>]+>$)/);
        lines.push(indent + open);
        walk(n, indent + "  ");
        lines.push(indent + close);
      } else lines.push(indent + n.outerHTML);
    });
    flush();
  };
  walk(t.content, "");
  return lines.join("\n");
}
function setSource(html) { $("#source").value = prettyHtml(html); colorSource(); }
/* Syntax colors for the Source view: the same text as the textarea, as colored spans in the <pre>
   underneath it (see .source-wrap in style.css). Pages are small, so it is redone on every input. */
const SX = /(<!--[\s\S]*?-->)|(<\/?)([a-zA-Z][\w:-]*)((?:"[^"]*"|'[^']*'|[^<>"'])*)(>)|(&[#\w]+;)/g;
const SX_ATTR = /([^\s=\/"']+)(?:(\s*=\s*)("[^"]*"|'[^']*'|[^\s"']+))?/g;
function colorSource() {
  const span = (cls, s) => `<span class="sx-${cls}">${escapeHtml(s)}</span>`;
  const src = $("#source").value;
  let out = "", last = 0;
  for (const m of src.matchAll(SX)) {
    out += escapeHtml(src.slice(last, m.index)); last = m.index + m[0].length;
    if (m[1]) out += span("com", m[1]);
    else if (m[6]) out += span("ent", m[6]);
    else {
      let attrs = "", at = 0;
      for (const a of m[4].matchAll(SX_ATTR)) {
        attrs += escapeHtml(m[4].slice(at, a.index)) + span("attr", a[1]) + (a[2] ? span("p", a[2]) + span("str", a[3]) : "");
        at = a.index + a[0].length;
      }
      out += span("p", m[2]) + span("tag", m[3]) + attrs + escapeHtml(m[4].slice(at)) + span("p", m[5]);
    }
  }
  $("#source-colors").innerHTML = out + escapeHtml(src.slice(last)) + "\n";  // a final newline needs a line to show on
  syncSourceScroll();
}
function syncSourceScroll() {
  const ta = $("#source"), pre = $("#source-colors");
  pre.scrollTop = ta.scrollTop; pre.scrollLeft = ta.scrollLeft;
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
    const saved = await tracked(persistPage(n, formSnapshot(), { approve, overwrite }));
    state.dirty = false; clearDraft(n); state.pageData = { ...state.pageData, ...saved };
    $("#editor").innerHTML = saved.html; showFigures($("#editor")); setSource(saved.html);
    refreshDiffSoon(0);
    const p = state.doc.pages.find((x) => x.index === n);
    Object.assign(p, saved);
    renderPages(); renderMeta();
    setStatus(`${approve ? "Approved" : "Saved"} page ${n} (${saved.words} words)`);
    reportView();
    if (andNext) {
      const next = neighbourPage(n, 1);
      if (next) loadPage(next);
      else if (reviewFilterOn()) setStatus(`Approved page ${n}. No pages after it still need work.`);
    }
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

function markDirty() { if (!state.dirty) { state.dirty = true; reportView(); renderPages(); renderMeta(); } storeDraftSoon(); refreshDiffSoon(); }

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
    const gen = state.gen;
    const fresh = await api(`/documents/${state.doc.doc_id}`);
    if (state.busy || state.gen !== gen) return;  // a save or page load overlapped: snapshot may be stale
    state.doc.pages = fresh.pages; state.doc.status_counts = fresh.status_counts;
    showProperties(fresh);
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
/* Chrome's block commands leave a new list inside the paragraph it replaced and wrap moved text in
   spans carrying the old computed style; unwrap both, keeping the caret where it was. */
function tidyAfterCommand() {
  const unwrap = (el) => { while (el.firstChild) el.parentNode.insertBefore(el.firstChild, el); el.remove(); };
  const sel = window.getSelection();
  const at = sel && sel.rangeCount ? [sel.anchorNode, sel.anchorOffset, sel.focusNode, sel.focusOffset] : null;
  $("#editor").querySelectorAll("span[style]:not([lang])").forEach(unwrap);
  $("#editor").querySelectorAll("p > ul, p > ol").forEach((list) => {
    const par = list.parentElement;
    if ([...par.childNodes].every((n) => n === list || !n.textContent.trim())) unwrap(par);
  });
  // Moving a node drops the selection out of it; the text nodes themselves survive, so put it back.
  if (at && at[0].isConnected && at[2].isConnected) sel.setBaseAndExtent(...at);
}
document.querySelectorAll("[data-cmd]").forEach((b) => b.addEventListener("click", () => {
  $("#editor").focus(); document.execCommand(b.dataset.cmd, false, null); tidyAfterCommand(); markDirty(); updateBlockStyle();
}));

// The block-style dropdown shows the element type at the caret and converts the block when changed.
// A list item reports its list, and text inside a block quote or footnote reports that container,
// because that is the type a reader of the output will meet.
const BLOCK_KINDS = "p, h1, h2, h3, h4, h5, h6, pre, td, th, caption, figcaption, figure, dt, dd";
const LIST_CMD = { ul: "insertUnorderedList", ol: "insertOrderedList" };
function blockKindAt(node) {
  const ed = $("#editor");
  const el = node && (node.nodeType === 1 ? node : node.parentElement);
  if (!el || !ed.contains(el)) return "";
  const inside = (sel) => { const hit = el.closest(sel); return hit && hit !== ed && ed.contains(hit) ? hit : null; };
  const li = inside("li");
  if (li && li.parentElement) return li.parentElement.tagName.toLowerCase();
  if (inside("blockquote")) return "blockquote";
  if (inside('aside[role="doc-footnote"]')) return "footnote";
  const block = inside(BLOCK_KINDS);
  return block ? block.tagName.toLowerCase() : "p";  // bare text in the editor is saved as a paragraph
}
function currentBlockKind() {
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount || !$("#editor").contains(sel.anchorNode)) return null;
  const range = sel.getRangeAt(0);
  const start = blockKindAt(range.startContainer);
  return sel.isCollapsed || blockKindAt(range.endContainer) === start ? start : "mixed";
}
function updateBlockStyle() {
  const kind = currentBlockKind();
  if (kind !== null) $("#block-style").value = kind;  // selection elsewhere: keep showing the last type
}
document.addEventListener("selectionchange", updateBlockStyle);

$("#block-style").addEventListener("change", (e) => {
  const target = e.target.value;
  $("#editor").focus();  // brings back the editor selection the dropdown took focus from
  const from = currentBlockKind();
  if (["caption", "figcaption", "dt", "figure"].includes(from)) {
    setStatus("This text's type cannot be changed here; use Source to restructure it.", true);
    updateBlockStyle(); return;
  }
  // Leave the current container first, so a heading does not end up inside a list item or a quote.
  if (LIST_CMD[from] && !LIST_CMD[target]) document.execCommand(LIST_CMD[from], false, null);
  if (from === "blockquote" && target !== "blockquote") document.execCommand("outdent", false, null);
  if (LIST_CMD[target]) {
    if (!LIST_CMD[from]) document.execCommand("formatBlock", false, "p");  // a heading would stay inside the item
    if (currentBlockKind() !== target) document.execCommand(LIST_CMD[target], false, null);
  } else {
    document.execCommand("formatBlock", false, target);
  }
  tidyAfterCommand(); markDirty(); updateBlockStyle();
});

// The symbol dropdown types the chosen character at the caret (over the selection, if any), in the
// editor or in Source, whichever is showing. insertText keeps it on the browser's undo stack.
$("#symbol-picker").addEventListener("change", (e) => {
  const ch = e.target.value;
  e.target.value = "";  // back to the Ω label, so the same symbol can be chosen twice running
  if (!ch) return;
  const target = $("#source").hidden ? $("#editor") : $("#source");
  if (target.hidden) return;  // Draft view: nothing editable is showing
  target.focus();  // brings back the selection the dropdown took focus from
  document.execCommand("insertText", false, ch);
  markDirty(); updateBlockStyle();
});

// Alignment is a class on the paragraph, heading, table cell or figure; left is the default, so it just
// removes the class, except on a wrapped figure, where align-left is what floats it, and on a <th>,
// which browsers center unless told otherwise.
const ALIGNABLE = "p, h1, h2, h3, h4, h5, h6, th, td";
function selectedAlignBlocks() {
  const fig = figureWrapper(selectedImg);  // a figure clicked in the editor takes precedence
  if (fig) return [fig];
  const sel = window.getSelection();
  const ed = $("#editor");
  if (!sel || !sel.rangeCount || !ed.contains(sel.anchorNode)) return [];
  const range = sel.getRangeAt(0);
  if (!sel.isCollapsed) {
    // Every range, not just the first: Firefox selects dragged-over table cells as one range per cell.
    const ranges = Array.from({ length: sel.rangeCount }, (_, i) => sel.getRangeAt(i));
    const hit = [...ed.querySelectorAll(ALIGNABLE)].filter((b) => ranges.some((r) => r.intersectsNode(b)));
    if (hit.length) return hit;
  }
  const node = range.startContainer;
  const block = (node.nodeType === 1 ? node : node.parentElement).closest(ALIGNABLE);
  return block && ed.contains(block) ? [block] : [];
}
function updateAlignButtons() {
  const blocks = selectedAlignBlocks();
  // A header cell without a class may be centered (column heading) or left (row heading): ask the browser.
  const current = (b) => ["align-center", "align-right"].find((c) => b.classList.contains(c)) ||
    (b.tagName === "TH" && !b.classList.contains("align-left") && getComputedStyle(b).textAlign.includes("center") ? "align-center" : "");
  document.querySelectorAll("[data-align]").forEach((btn) => btn.setAttribute("aria-pressed",
    String(blocks.length > 0 && blocks.every((b) => current(b) === btn.dataset.align))));
}
document.querySelectorAll("[data-align]").forEach((btn) => {
  btn.addEventListener("mousedown", (e) => e.preventDefault());  // keep the editor selection
  btn.addEventListener("click", () => {
    const blocks = selectedAlignBlocks();
    if (!blocks.length) { setStatus("Put the caret in a paragraph, heading or table cell, or click a figure, to align it."); return; }
    blocks.forEach((b) => {
      const wrapped = b.classList.contains("wrap");
      b.classList.remove("align-left", "align-center", "align-right");
      if (btn.dataset.align) b.classList.add(btn.dataset.align);
      else if (wrapped || b.tagName === "TH") b.classList.add("align-left");
      if (wrapped && btn.dataset.align === "align-center") {
        b.classList.remove("wrap"); $("#fig-wrap").checked = false;
        setStatus("Centered figures don't wrap text; wrap turned off.");
      }
      if (!b.classList.length) b.removeAttribute("class");
    });
    updateAlignButtons(); markDirty();
  });
});
document.addEventListener("selectionchange", updateAlignButtons);

$("#editor").addEventListener("input", markDirty);
$("#source").addEventListener("input", markDirty);
$("#source").addEventListener("input", colorSource);
$("#source").addEventListener("scroll", syncSourceScroll);
["#f-label", "#f-starts", "#f-ends", "#f-skip", "#f-notes"].forEach((s) => $(s).addEventListener("change", markDirty));

// ------------------------------------------------------------------ figure panel (alt text, AI autofill, wrap)
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
  // A bare image straight under the editor (+Fig with no caret in the editor, e.g. right after F2 loads
  // a page) has nothing to carry its alignment: give it the paragraph a browser would have made.
  if (imgEl && imgEl.parentElement === $("#editor")) {
    const p = document.createElement("p");
    imgEl.replaceWith(p); p.append(imgEl); markDirty();
  }
  selectedImg = imgEl;
  updateAlignButtons();
  if (!imgEl) { $("#figure-panel").hidden = true; cropBox.hidden = true; return; }
  imgEl.classList.add("selected");
  const fig = figureWrapper(imgEl);
  if (fig) fig.classList.add("selected-figure");
  $("#figure-label").textContent = "Figure" + (imgEl.dataset.fig ? " " + imgEl.dataset.fig : "");
  $("#fig-alt").value = imgEl.getAttribute("alt") || "";
  $("#fig-wrap").checked = !!fig && fig.classList.contains("wrap");
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
  if (!f) { f = { id: fid, alt: selectedImg.getAttribute("alt") || "", bbox: null, caption: "", rotate: 0 }; figs.push(f); }
  f.bbox = [box.x0, box.y0, box.x1, box.y1].map((v) => Math.round(v * 10) / 10);
  selectedImg.src = figurePreviewSrc(f);
  $("#figure-label").textContent = "Figure " + fid;
  $("#fig-ai").disabled = false;
  $("#fig-crop-hint").textContent = "Crop updated; Save to keep it.";
  showCropBox(box);
  if (state.mark || state.follow) { lastLocateKey = "figure:" + fid; placeMarker(box); }
  markDirty();
}
/* The editor preview of a figure record's (possibly unsaved) crop box and rotation. */
function figurePreviewSrc(f) {
  return `/api/documents/${state.doc.doc_id}/pages/${state.page}/figure/${encodeURIComponent(f.id)}` +
    `?bbox=${f.bbox.join(",")}&rotate=${f.rotate || 0}`;
}
/* Turn the selected figure a quarter turn (delta = 90 clockwise, -90 counterclockwise). The rotation is
   stored on the figure record and applied when the crop is cut out, so the output image is rotated too. */
function rotateFigure(delta) {
  const fid = selectedImg && selectedImg.dataset.fig;
  const f = fid && (state.pageData.figures || []).find((x) => x.id === fid);
  if (!f || !f.bbox) { setStatus("Draw a crop box for this figure before rotating it.", true); return; }
  f.rotate = (((f.rotate || 0) + delta) % 360 + 360) % 360;
  selectedImg.src = figurePreviewSrc(f);
  setStatus(`Figure rotated to ${f.rotate}°; Save to keep it.`);
  markDirty();
}
$("#fig-rotate-cw").addEventListener("click", () => rotateFigure(90));
$("#fig-rotate-ccw").addEventListener("click", () => rotateFigure(-90));
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
  disarmDrawTable();
  const drawn = d.moved && d.current && d.current.x1 - d.current.x0 >= 5 && d.current.y1 - d.current.y0 >= 5;
  if (drawn && d.table) readTable(d.current, d.table);
  else if (drawn) commitCrop(d.current);
  else if (d.mode === "draw") showCropBox(figureBox(selectedImg && selectedImg.dataset.fig));
  state.suppressClick = d.moved;  // the click event that follows a drag must not jump to a word
});
/* Draw mode: the next drag on the scan defines the selected figure's box. */
function armDrawCrop() {
  if (!selectedImg) { setStatus("Select a figure in the editor first.", true); return; }
  disarmDrawTable();
  state.drawCrop = true; scroller.classList.add("drawing");
  setStatus("Drag over the scan to draw the figure's crop box.");
}
$("#fig-draw").addEventListener("click", armDrawCrop);
scroller.addEventListener("mousedown", (e) => {
  if (state.drawTable && state.drawTable.page !== state.page) disarmDrawTable();  // armed on another page
  if (!(state.drawCrop || state.drawTable) || e.target !== img) return;
  e.preventDefault();
  cropDrag = { mode: "draw", start: pagePoint(e), box: null, moved: false, table: state.drawTable };
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
  // No caret in a block (e.g. right after F2 loads a page): go after the last paragraph or heading, so the
  // browser doesn't drop the image into that paragraph's text or loose under the editor.
  if (!block && ed.lastElementChild && ed.lastElementChild.matches(BLOCK_SEL)) block = ed.lastElementChild;
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
// ------------------------------------------------------------------ +Table: re-read a region of the scan as a table
/* For a table the transcriber ran together as text. +Table arms a draw on the scan; the model reads the
   drawn region as a <table>, which replaces the editor selection (select the run-together text first)
   or, with no selection, goes in after the block holding the caret. Nothing is stored until Save. */
function disarmDrawTable() {
  state.drawTable = null; scroller.classList.remove("drawing");
  $("#btn-insert-table").setAttribute("aria-pressed", "false");
}
$("#btn-insert-table").addEventListener("mousedown", (e) => e.preventDefault());  // keep the editor selection
$("#btn-insert-table").addEventListener("click", () => {
  if (state.drawTable) { disarmDrawTable(); setStatus("Table drawing cancelled."); return; }
  if (!state.doc || !state.pageData) return;
  const ed = $("#editor"), sel = window.getSelection();
  const inEditor = sel && sel.rangeCount && ed.contains(sel.getRangeAt(0).commonAncestorContainer);
  const range = inEditor ? sel.getRangeAt(0).cloneRange() : null;
  selectFigure(null);  // the crop box on the scan is about to show the table region
  state.drawCrop = false;
  state.drawTable = { range, text: range && !range.collapsed ? sel.toString() : "", page: state.page, doc: state.doc.doc_id };
  scroller.classList.add("drawing");
  $("#btn-insert-table").setAttribute("aria-pressed", "true");
  setStatus(range && !range.collapsed ? "Drag over the table on the scan; it will replace the selected text."
    : "Drag over the table on the scan; it will be inserted after the caret's paragraph. (Select the run-together text first to replace it.)");
});
async function readTable(box, target) {
  const btn = $("#btn-insert-table");
  showCropBox(box); btn.disabled = true; setStatus("Asking the model to read the region as a table…");
  try {
    const r = await api(`/documents/${target.doc}/pages/${target.page}/table`, { method: "POST",
      body: { bbox: [box.x0, box.y0, box.x1, box.y1].map((v) => Math.round(v * 10) / 10), text: target.text } });
    if (!state.doc || state.doc.doc_id !== target.doc || state.page !== target.page) {
      setStatus("The table came back after you left the page; it was not inserted.", true); return;
    }
    insertTable(r.html, target.range);
    setStatus(`Table inserted (${r.model}). Check it against the scan; Ctrl+Z undoes it.`);
  } catch (e) { setStatus("Table failed: " + e.message, true); }
  finally { btn.disabled = false; if (!selectedImg) cropBox.hidden = true; }
}
/* Through execCommand, like +Fig, so Ctrl+Z takes the table out again (and brings replaced text back). */
function insertTable(html, range) {
  const ed = $("#editor"); ed.focus();
  const sel = window.getSelection();
  let r = range && ed.contains(range.commonAncestorContainer) ? range : null;  // the text may have been edited away meanwhile
  let block = null;
  if (!r || r.collapsed) {
    if (r) {
      const at = r.startContainer.nodeType === Node.TEXT_NODE ? r.startContainer.parentElement : r.startContainer;
      // Never inside another table or a list: go after the whole thing.
      block = at.closest("table, ul, ol, dl") || at.closest(BLOCK_SEL);
      while (block && block.parentElement && block.parentElement !== ed && block.parentElement.closest("table, ul, ol, dl")) block = block.parentElement.closest("table, ul, ol, dl");
      if (block === ed || !ed.contains(block)) block = null;
    }
    r = document.createRange();
    if (block && block.matches("table, ul, ol, dl")) { r.setStartAfter(block); r.collapse(true); block = null; }
    else { r.selectNodeContents(block || ed); r.collapse(false); }
  }
  sel.removeAllRanges(); sel.addRange(r);
  if (block) document.execCommand("insertParagraph");
  if (!document.execCommand("insertHTML", false, html)) {  // fallback for browsers without insertHTML
    const t = document.createElement("template"); t.innerHTML = html; r.deleteContents(); r.insertNode(t.content);
  }
  markDirty();
}

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
// Wrap floats the figure to its side (align-left or align-right, set with the toolbar alignment buttons).
$("#fig-wrap").addEventListener("change", () => {
  const fig = figureWrapper(selectedImg);
  if (!fig) return;
  if (!$("#fig-wrap").checked) {
    fig.classList.remove("wrap", "align-left");  // left is the default without wrap
  } else if (fig.classList.contains("align-center")) {
    $("#fig-wrap").checked = false;
    setStatus("Wrap needs a left or right aligned figure; use the alignment buttons first.");
    return;
  } else {
    if (!fig.classList.contains("align-right")) fig.classList.add("align-left");
    fig.classList.add("wrap");
  }
  if (!fig.classList.length) fig.removeAttribute("class");
  updateAlignButtons(); markDirty();
});
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
// A numbered list continued from the previous page starts at its printed number (the number of the
// item the page opens in the middle of, if it does); the build joins the two lists.
$("#btn-list-start").addEventListener("click", () => {
  const sel = window.getSelection();
  const node = sel && sel.rangeCount ? sel.anchorNode : null;
  const el = node && (node.nodeType === 1 ? node : node.parentElement);
  const list = el && el.closest("ol, ul");
  if (!list || list.tagName !== "OL" || !$("#editor").contains(list)) { setStatus("Put the caret in a numbered list first.", true); return; }
  const answer = prompt("Number of the first item of this list on this page (3 for c or iii):", list.getAttribute("start") || "1");
  if (answer === null) return;
  if (!/^\s*-?\d+\s*$/.test(answer)) { setStatus("The start must be a whole number.", true); return; }
  const n = parseInt(answer, 10);
  if (n === 1) list.removeAttribute("start"); else list.setAttribute("start", n);
  markDirty();
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
  if (src) { setSource(editorHtml()); } else { $("#editor").innerHTML = $("#source").value; showFigures($("#editor")); }
  $("#source").hidden = !src; $("#editor").hidden = src;
  if (src) { $("#toggle-draft").checked = false; $("#draft").hidden = true; }
  refreshDiffSoon(0);
});
$("#toggle-draft").addEventListener("change", (e) => {
  $("#draft").hidden = !e.target.checked;
  if (e.target.checked) { $("#editor").hidden = true; $("#source").hidden = true; $("#toggle-source").checked = false; }
  else { $("#editor").hidden = false; }
});
$("#btn-approve").addEventListener("click", () => savePage(false, false, true));
$("#btn-approve-next").addEventListener("click", () => savePage(true, false, true));
$("#btn-save-all").addEventListener("click", saveAll);
$("#btn-prev").addEventListener("click", () => { const m = state.doc && state.page && neighbourPage(state.page, -1); if (m) loadPage(m); });
$("#btn-next").addEventListener("click", () => { const m = state.doc && state.page && neighbourPage(state.page, 1); if (m) loadPage(m); });
try { $("#filter-review").checked = localStorage.getItem("unscanner.filterReview") === "1"; } catch (_) { /* storage disabled */ }
$("#filter-review").addEventListener("change", () => {
  try { localStorage.setItem("unscanner.filterReview", reviewFilterOn() ? "1" : "0"); } catch (_) { /* storage disabled */ }
  renderPages();
  $(`#page-list li[data-page="${state.page}"]`)?.scrollIntoView({ block: "nearest" });
});
$("#page-list").addEventListener("click", (e) => { const li = e.target.closest("li[data-page]"); if (li) loadPage(+li.dataset.page); });
$("#doc-select").addEventListener("change", (e) => openDoc(e.target.value));
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); saveAll(); }
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); savePage(false, false, true); }
  if (e.altKey && e.key === "ArrowLeft") { e.preventDefault(); $("#btn-prev").click(); }
  if (e.altKey && e.key === "ArrowRight") { e.preventDefault(); $("#btn-next").click(); }
  // F2 = Approve & next: one left-hand key that types nothing into the editor (and, not being a
  // character key, needs no off switch under WCAG 2.1.4). A held key approves only one page.
  if (e.key === "F2" && !e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey) {
    e.preventDefault();
    if (!e.repeat) $("#btn-approve-next").click();  // a disabled button ignores the click
  }
});
window.addEventListener("beforeunload", () => storeDraftNow());  // drafts survive a reload

// ------------------------------------------------------------------ dialogs
function wireDialog(id) {
  const dlg = $(id);
  dlg.querySelector(".dlg-cancel").addEventListener("click", () => dlg.close("cancel"));
  return dlg;
}
const dlgOpen = wireDialog("#dlg-open"), dlgTranscribe = wireDialog("#dlg-transcribe"), dlgSettings = wireDialog("#dlg-settings");
const dlgProperties = wireDialog("#dlg-properties");
// Shown over the editor pane only, so the scan and page list stay usable for reading the title page.
function showInEditorPane(dlg) {
  const pane = dlg.parentElement;
  [...pane.children].forEach((el) => { if (el !== dlg) el.inert = true; });
  pane.classList.add("dialog-open");
  dlg.show();
  dlg.querySelector("input, select, textarea").focus();
}
dlgProperties.addEventListener("close", () => {
  const pane = dlgProperties.parentElement;
  [...pane.children].forEach((el) => (el.inert = false));
  pane.classList.remove("dialog-open");
});
dlgProperties.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); dlgProperties.close("cancel"); } });

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

// Export / import: the whole project as one file, for backups and for moving to another computer.
$("#btn-export").addEventListener("click", async () => {
  if (state.dirty) await savePage(false);
  if (Object.keys(docDrafts()).length) await saveAll();
  const left = Object.keys(docDrafts()).length;
  const a = document.createElement("a");
  a.href = `/api/documents/${state.doc.doc_id}/export`; a.download = "";
  document.body.append(a); a.click(); a.remove();
  setStatus(left ? `Exported the project without ${left} page(s) whose edits could not be saved` : "Exported the project; keep the file next to the PDF", !!left);
});

const dlgImport = wireDialog("#dlg-import");
$("#btn-import").addEventListener("click", () => { $("#form-import").reset(); dlgImport.showModal(); });
$("#form-import").addEventListener("submit", async (e) => {
  const f = new FormData(e.target);
  const send = (replace) => {
    const fd = new FormData();
    fd.append("project_file", f.get("project_file"));
    if (f.get("pdf") && f.get("pdf").size > 0) fd.append("pdf", f.get("pdf"));
    fd.append("pdf_path", f.get("pdf_path") || "");
    fd.append("replace", replace ? "true" : "false");
    return api("/projects/import", { method: "POST", body: fd });
  };
  try {
    let doc;
    try { doc = await send(false); } catch (err) {
      if (err.status !== 409 || !confirm(`${err.message}. Replace it with the imported project? The pages stored here now, and any unsaved edits to them, will be lost.`)) throw err;
      doc = await send(true);
      const d = allDrafts(); delete d[doc.doc_id]; writeDrafts(d);  // they were edits to the replaced pages
      if (state.doc && state.doc.doc_id === doc.doc_id) state.dirty = false;
    }
    await loadDocs(doc.doc_id);
    setStatus(`Imported ${doc.title} (${doc.page_count} pages)`);
  } catch (err) { setStatus("Import failed: " + err.message, true); }
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

$("#btn-properties").addEventListener("click", async () => {
  if (dlgProperties.open) { dlgProperties.querySelector("input").focus(); return; }  // keep what was typed
  const d = await api(`/documents/${state.doc.doc_id}`);  // fresh: Claude may have changed them
  const f = $("#form-properties");
  ["title", "author", "language"].forEach((k) => (f.elements[k].value = d[k] || ""));
  f.dataset.docId = d.doc_id;  // the page list stays usable, so remember which document this is
  showInEditorPane(dlgProperties);
});
function showProperties(d) {
  Object.assign(state.doc, { title: d.title, author: d.author, language: d.language });
  const opt = $(`#doc-select option[value="${d.doc_id}"]`);
  if (opt) opt.textContent = d.title || d.doc_id;
}
$("#form-properties").addEventListener("submit", async (e) => {
  const f = new FormData(e.target);
  try {
    const d = await api(`/documents/${e.target.dataset.docId}/properties`, { method: "PUT",
      body: { title: f.get("title"), author: f.get("author"), language: f.get("language") } });
    showProperties(d);
    setStatus(`Properties saved: ${d.title} · ${d.author || "no author"} · ${d.language}. Build again to update the output.`);
  } catch (err) { setStatus("Properties not saved: " + err.message, true); }
});

$("#btn-settings").addEventListener("click", async () => {
  const s = await api("/settings");
  const f = $("#form-settings");
  Object.entries(s).forEach(([k, v]) => {
    const el = f.elements[k];
    if (!el) return;
    if (el.type === "checkbox") el.checked = !!v; else el.value = v;
  });
  // The server never sends a saved API key back: the field stays empty and only says one is saved.
  ["anthropic_api_key", "openai_api_key"].forEach((k) => {
    f.elements[k].placeholder = s[k + "_set"] ? "saved (type to replace)" : "";
    f.elements[k + "_forget"].checked = false;
    f.elements[k + "_forget"].disabled = !s[k + "_set"];
  });
  const where = s.key_storage === "keyring"
    ? "Keys are kept in this computer's credential store (Windows Credential Manager, macOS Keychain, or the Linux keyring), not in a file."
    : "Keys are kept in plain text in work/settings.json. Install the keyring package (pip install keyring) and restart to keep them in the operating system's credential store instead.";
  f.querySelectorAll(".key-storage").forEach((el) => (el.textContent = where));
  dlgSettings.showModal();
});
$("#form-settings").addEventListener("submit", async (e) => {
  const f = new FormData(e.target); const body = {};
  f.forEach((v, k) => (body[k] = k === "workers" || k === "openai_max_tokens" ? +v : v));
  // An unticked checkbox is absent from FormData; send it explicitly.
  body.send_title = e.target.elements.send_title.checked;
  body.openai_disable_thinking = e.target.elements.openai_disable_thinking.checked;
  // An empty key field keeps the saved key; null tells the server to forget it.
  ["anthropic_api_key", "openai_api_key"].forEach((k) => {
    if (body[k + "_forget"]) body[k] = null;
    delete body[k + "_forget"];
  });
  try { await api("/settings", { method: "PUT", body }); setStatus("Settings saved"); }
  catch (err) { setStatus("Settings not saved: " + err.message, true); }
});

function escapeHtml(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

// ------------------------------------------------------------------ start
loadDocs(localStorage.getItem("unscanner.lastDoc") || "").catch((e) => setStatus(e.message, true));
