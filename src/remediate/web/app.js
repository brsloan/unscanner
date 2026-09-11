/* remediate UI: plain JS, no build step. Talks to the JSON API in webapp.py. */
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = { docs: [], doc: null, page: null, pageData: null, dirty: false, job: null, settings: null };

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
    throw new Error(msg);
  }
  return res.json();
}

function setStatus(text, isError = false) {
  const el = $("#status");
  el.textContent = text;
  el.style.color = isError ? "var(--err)" : "";
}

// ------------------------------------------------------------------ documents
async function loadDocs(selectId) {
  state.docs = await api("/documents");
  const sel = $("#doc-select");
  sel.innerHTML = '<option value="">— choose a document —</option>' +
    state.docs.map((d) => `<option value="${d.doc_id}">${escapeHtml(d.title || d.doc_id)}</option>`).join("");
  if (selectId) { sel.value = selectId; await openDoc(selectId); }
}

async function openDoc(docId) {
  if (!docId) { state.doc = null; renderPages(); return; }
  state.doc = await api(`/documents/${docId}`);
  localStorage.setItem("remediate.lastDoc", docId);
  renderPages();
  const s = state.doc.status_counts || {};
  $("#doc-meta").textContent = `${state.doc.page_count} pages · ${s.done || 0} done · ${s.needs_review || 0} to review · ${s.pending || 0} pending`;
  ["#btn-transcribe", "#btn-build", "#btn-validate"].forEach((b) => ($(b).disabled = false));
  $("#lnk-html").href = `/api/documents/${docId}/output/html`;
  $("#lnk-epub").href = `/api/documents/${docId}/output/epub`;
  if (state.doc.job) pollJob(state.doc.job.id);
  const first = (state.doc.pages.find((p) => p.status === "needs_review") || state.doc.pages[0]);
  if (first) loadPage(first.index);
}

function renderPages() {
  const list = $("#page-list");
  if (!state.doc) { list.innerHTML = ""; return; }
  list.innerHTML = state.doc.pages.map((p) => `
    <li data-page="${p.index}" aria-current="${p.index === state.page}" title="${escapeHtml(p.notes || p.status)}">
      <span class="dot ${p.status}" aria-hidden="true"></span>
      <span>Page ${p.index}${p.skip ? " (skipped)" : ""}</span>
      <span class="lbl">${p.label ? "p. " + escapeHtml(p.label) : ""}</span>
      <span class="visually-hidden">${p.status}</span>
    </li>`).join("");
}

// ------------------------------------------------------------------ pages
async function loadPage(n) {
  if (state.dirty && !confirm("Discard unsaved changes on this page?")) return;
  const d = await api(`/documents/${state.doc.doc_id}/pages/${n}`);
  state.page = n; state.pageData = d; state.dirty = false;
  $("#page-indicator").textContent = `PDF page ${n} of ${d.of}${d.label ? " · printed " + d.label : ""}`;
  const img = $("#page-image");
  img.hidden = false;
  img.src = `/documents/${state.doc.doc_id}/pages/${n}/image`.replace(/^/, "/api");
  $("#editor").innerHTML = d.html || "";
  $("#source").value = d.html || "";
  $("#draft").textContent = d.draft_text || "(no draft text)";
  $("#f-label").value = d.label || "";
  $("#f-starts").checked = !!d.starts_mid_paragraph;
  $("#f-ends").checked = !!d.ends_mid_paragraph;
  $("#f-skip").checked = !!d.skip;
  $("#f-notes").value = d.notes || "";
  $("#btn-save").disabled = false; $("#btn-save-next").disabled = false;
  renderPages();
  setStatus(d.status === "needs_review" && d.notes ? "Model note: " + d.notes : `Page ${n}: ${d.status}`);
  $("#editor").scrollTop = 0;
}

function currentHtml() {
  return $("#toggle-source").checked ? $("#source").value : $("#editor").innerHTML;
}

async function savePage(andNext = false) {
  if (!state.doc || !state.page) return;
  const body = {
    html: currentHtml(), label: $("#f-label").value.trim() || null,
    starts_mid_paragraph: $("#f-starts").checked, ends_mid_paragraph: $("#f-ends").checked,
    skip: $("#f-skip").checked, notes: $("#f-notes").value.trim(), status: "done",
  };
  try {
    const saved = await api(`/documents/${state.doc.doc_id}/pages/${state.page}`, { method: "PUT", body });
    state.dirty = false;
    $("#editor").innerHTML = saved.html; $("#source").value = saved.html;
    const p = state.doc.pages.find((x) => x.index === state.page);
    Object.assign(p, saved);
    renderPages();
    setStatus(`Saved page ${state.page} (${saved.words} words)`);
    if (andNext && state.page < state.doc.page_count) loadPage(state.page + 1);
  } catch (e) { setStatus("Save failed: " + e.message, true); }
}

function markDirty() { state.dirty = true; }

// ------------------------------------------------------------------ editor commands
document.execCommand("defaultParagraphSeparator", false, "p");
document.querySelectorAll("[data-cmd]").forEach((b) => b.addEventListener("click", () => {
  $("#editor").focus(); document.execCommand(b.dataset.cmd, false, null); markDirty();
}));
$("#block-style").addEventListener("change", (e) => {
  $("#editor").focus(); document.execCommand("formatBlock", false, e.target.value); markDirty();
});
$("#editor").addEventListener("input", markDirty);
$("#source").addEventListener("input", markDirty);
["#f-label", "#f-starts", "#f-ends", "#f-skip", "#f-notes"].forEach((s) => $(s).addEventListener("change", markDirty));

let selectedImg = null;
$("#editor").addEventListener("click", (e) => {
  document.querySelectorAll("#editor img.selected").forEach((i) => i.classList.remove("selected"));
  selectedImg = e.target.tagName === "IMG" ? e.target : null;
  if (selectedImg) selectedImg.classList.add("selected");
});
$("#btn-alt").addEventListener("click", () => {
  if (!selectedImg) { setStatus("Click an image in the editor first.", true); return; }
  const alt = prompt("Alternative text for this image (describe what it shows and why it matters):", selectedImg.alt || "");
  if (alt !== null) { selectedImg.alt = alt; markDirty(); }
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
  if (src) { $("#source").value = $("#editor").innerHTML; } else { $("#editor").innerHTML = $("#source").value; }
  $("#source").hidden = !src; $("#editor").hidden = src;
  if (src) $("#toggle-draft").checked = false, $("#draft").hidden = true;
});
$("#toggle-draft").addEventListener("change", (e) => {
  $("#draft").hidden = !e.target.checked;
  if (e.target.checked) { $("#editor").hidden = true; $("#source").hidden = true; $("#toggle-source").checked = false; }
  else { $("#editor").hidden = false; }
});
$("#zoom").addEventListener("input", (e) => { $("#page-image").style.width = e.target.value + "%"; });

$("#btn-save").addEventListener("click", () => savePage(false));
$("#btn-save-next").addEventListener("click", () => savePage(true));
$("#btn-prev").addEventListener("click", () => state.page > 1 && loadPage(state.page - 1));
$("#btn-next").addEventListener("click", () => state.doc && state.page < state.doc.page_count && loadPage(state.page + 1));
$("#page-list").addEventListener("click", (e) => { const li = e.target.closest("li[data-page]"); if (li) loadPage(+li.dataset.page); });
$("#doc-select").addEventListener("change", (e) => openDoc(e.target.value));
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); savePage(false); }
  if (e.altKey && e.key === "ArrowLeft") { e.preventDefault(); $("#btn-prev").click(); }
  if (e.altKey && e.key === "ArrowRight") { e.preventDefault(); $("#btn-next").click(); }
});
window.addEventListener("beforeunload", (e) => { if (state.dirty) { e.preventDefault(); e.returnValue = ""; } });

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
  $("#transcribe-backend").textContent = s.backend === "anthropic"
    ? `Using Claude API, model ${s.model}, effort ${s.effort}.` : `Using local endpoint ${s.openai_base_url}, model ${s.openai_model}.`;
  dlgTranscribe.showModal();
});
$("#form-transcribe").addEventListener("submit", async (e) => {
  const f = new FormData(e.target);
  try {
    const job = await api(`/documents/${state.doc.doc_id}/transcribe`, { method: "POST", body: { pages: f.get("pages") || "all", force: !!f.get("force") } });
    pollJob(job.id);
  } catch (err) { setStatus("Could not start: " + err.message, true); }
});

async function pollJob(jobId) {
  const job = await api(`/jobs/${jobId}`);
  state.job = job;
  if (job.status === "running") {
    setStatus(`Transcribing with ${job.model}: ${job.completed}/${job.requested} pages${job.errors ? ", " + job.errors + " errors" : ""} — ${job.last}`);
    const fresh = await api(`/documents/${state.doc.doc_id}`);
    state.doc.pages = fresh.pages; state.doc.status_counts = fresh.status_counts; renderPages();
    setTimeout(() => pollJob(jobId), 2000);
  } else {
    const fresh = await api(`/documents/${state.doc.doc_id}`);
    state.doc = fresh; renderPages();
    if (job.status === "error") setStatus("Transcription failed: " + job.error, true);
    else {
      const u = job.summary.usage || {};
      setStatus(`Transcribed ${job.summary.done} pages (${job.summary.errors} errors) · ${u.input_tokens || 0} in / ${u.output_tokens || 0} out tokens`);
    }
    if (state.page && !state.dirty) loadPage(state.page);
  }
}

$("#btn-build").addEventListener("click", async () => {
  if (state.dirty) await savePage(false);
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
  Object.entries(s).forEach(([k, v]) => { if (f.elements[k]) f.elements[k].value = v; });
  dlgSettings.showModal();
});
$("#form-settings").addEventListener("submit", async (e) => {
  const f = new FormData(e.target); const body = {};
  f.forEach((v, k) => (body[k] = k === "workers" ? +v : v));
  try { await api("/settings", { method: "PUT", body }); setStatus("Settings saved"); }
  catch (err) { setStatus("Settings not saved: " + err.message, true); }
});

function escapeHtml(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

// ------------------------------------------------------------------ start
loadDocs(localStorage.getItem("remediate.lastDoc") || "").catch((e) => setStatus(e.message, true));
