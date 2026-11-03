"use strict";

const STORE_KEY = "acadadviser.search";
const STAGES = ["reading", "searching", "finalizing"];
const GROUPS = [
  ["reach", "Reach", "Competitive for your profile."],
  ["target", "Target", "A reasonable match for your profile."],
  ["safety", "Safety", "Your profile looks strong for these."],
  ["unclassified", "Not yet classified", "The AI did not assign a label."],
];

const $ = (id) => document.getElementById(id);
const form = $("profile-form");
const results = $("results");
let searchId = null;
let source = null;
let items = [];
let note = "";

function remember(id) {
  try {
    if (id) localStorage.setItem(STORE_KEY, id);
    else localStorage.removeItem(STORE_KEY);
  } catch { /* Storage can be blocked; the page still works for this visit. */ }
}

function recall() {
  try { return localStorage.getItem(STORE_KEY); } catch { return null; }
}

function showError(element, message) {
  element.textContent = message || "";
  element.hidden = !message;
}

async function api(path, options) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(typeof body.detail === "string" ? body.detail : "Something went wrong. Please try again.");
  }
  return body;
}

function setStage(stage) {
  const index = stage === "done" ? STAGES.length : STAGES.indexOf(stage);
  $("progress").hidden = stage === "done";
  document.querySelectorAll("#progress li").forEach((li, i) => {
    li.classList.toggle("done", i < index);
    li.classList.toggle("active", i === index);
  });
}

function setBusy(busy) {
  document.querySelectorAll("#actions button, #refine-text").forEach((el) => { el.disabled = busy; });
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function fact(list, term, value) {
  const row = el("div");
  row.append(el("dt", "", term), el("dd", "", String(value)));
  list.append(row);
}

function catalogBox(catalog) {
  const box = el("div", "catalog-box");
  box.append(el("p", "", "From our school catalog. Confirm on the official page."));
  const list = el("dl", "facts");
  if (catalog.duration) fact(list, "Duration", catalog.duration);
  if (catalog.tuition) {
    const basis = catalog.tuition.basis && catalog.tuition.basis !== "unknown" ? ` (${catalog.tuition.basis})` : "";
    fact(list, "Listed tuition", `${catalog.tuition.currency} ${Number(catalog.tuition.amount).toLocaleString("en-US")}${basis}`);
  }
  if (catalog.gre_policy) fact(list, "GRE", catalog.gre_policy.replace(/_/g, " "));
  if (catalog.minimum_gpa) fact(list, "Official minimum GPA", catalog.minimum_gpa);
  for (const [test, score] of Object.entries(catalog.english_minimums || {})) {
    fact(list, `Minimum ${test.toUpperCase()}`, typeof score === "object" ? JSON.stringify(score) : score);
  }
  if (catalog.next_deadline) fact(list, "Next listed deadline", catalog.next_deadline);
  if (catalog.community_gpa) {
    const size = catalog.community_gpa.sample_size ? `, ${catalog.community_gpa.sample_size} reports` : "";
    fact(list, `Self-reported admitted GPA median${size}`, catalog.community_gpa.median);
  }
  box.append(list);
  return box;
}

function safeUrl(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : null;
  } catch { return null; }
}

function card(item) {
  const bucket = GROUPS.some(([key]) => key === item.bucket) ? item.bucket : "unclassified";
  const article = el("article", "card");
  const head = el("div", "card-head");
  const title = el("div");
  title.append(el("h3", "", item.university_name || "University"), el("p", "program", item.display_name || ""));
  const drop = el("button", "drop", "Remove");
  drop.type = "button";
  drop.setAttribute("aria-label", `Remove ${item.university_name} ${item.display_name}`);
  drop.addEventListener("click", () => dropProgram(item.web_id, drop));
  head.append(title, drop);
  article.append(head, el("span", `bucket ${bucket}`, GROUPS.find(([key]) => key === bucket)[1]));
  if (item.why) article.append(el("p", "why", item.why));
  for (const warning of item.warnings || []) article.append(el("p", "warn", `Check: ${warning}`));
  const url = safeUrl(item.official_url);
  if (url) {
    const links = el("p", "links");
    const link = el("a", "", "Official program page");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    links.append(link);
    article.append(links);
  }
  if (item.catalog) article.append(catalogBox(item.catalog));
  return article;
}

function render() {
  const list = $("list");
  list.replaceChildren();
  for (const [key, label, hint] of GROUPS) {
    const rows = items.filter((item) => (GROUPS.some(([k]) => k === item.bucket) ? item.bucket : "unclassified") === key);
    if (!rows.length) continue;
    list.append(el("h3", "group-title", `${label} (${rows.length})`), el("p", "group-hint", hint));
    rows.forEach((item) => list.append(card(item)));
  }
  $("actions").hidden = !(items.length || note);
  $("download").hidden = !items.length;
  $("note").hidden = !note;
  $("note").textContent = note.replace(/\*\*/g, "");
}

function listen(id) {
  if (source) source.close();
  note = "";
  source = new EventSource(`/api/searches/${encodeURIComponent(id)}/events`);
  // Each connection replays the search's history, so note text starts clean.
  source.onopen = () => { note = ""; };
  source.onmessage = (message) => {
    const event = JSON.parse(message.data);
    if (event.type === "stage") {
      setStage(event.stage === "profile" ? "reading" : event.stage);
      setBusy(true);
    } else if (event.type === "note_delta") {
      note += event.delta;
    } else if (event.type === "turn_done") {
      note = event.text;
      setStage("done");
      setBusy(false);
      render();
    } else if (event.type === "list") {
      items = event.items;
      showError($("run-error"), "");
      render();
    } else if (event.type === "failed") {
      setStage("done");
      setBusy(false);
      showError($("run-error"), event.message);
      render();
    }
  };
}

function open(id) {
  searchId = id;
  remember(id);
  form.hidden = true;
  results.hidden = false;
  $("download").href = `/api/searches/${encodeURIComponent(id)}/report`;
}

function startRun(stage) {
  showError($("run-error"), "");
  note = "";
  setStage(stage);
  setBusy(true);
  render();
}

async function dropProgram(webId, button) {
  button.disabled = true;
  try {
    const body = await api(`/api/searches/${encodeURIComponent(searchId)}/drop`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ web_id: webId }),
    });
    items = body.items;
    render();
  } catch (error) {
    button.disabled = false;
    showError($("run-error"), error.message);
  }
}

async function followUp(path, options) {
  startRun("searching");
  try {
    await api(`/api/searches/${encodeURIComponent(searchId)}/${path}`, { method: "POST", ...options });
  } catch (error) {
    setStage("done");
    setBusy(false);
    showError($("run-error"), error.message);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = $("form-error");
  const data = new FormData(form);
  const hasResume = data.get("resume") && data.get("resume").size > 0;
  if (!hasResume && !String(data.get("background") || "").trim()) {
    showError(error, "Upload a resume or describe your background.");
    return;
  }
  if (!data.get("consent")) {
    showError(error, "Please accept the data notice to continue.");
    return;
  }
  showError(error, "");
  const submit = $("submit");
  submit.disabled = true;
  submit.textContent = "Reading your profile. This can take a minute…";
  try {
    const body = await api("/api/searches", { method: "POST", body: data });
    items = [];
    open(body.search_id);
    startRun("searching");
    listen(body.search_id);
    results.scrollIntoView();
  } catch (failure) {
    showError(error, failure.message);
  } finally {
    submit.disabled = false;
    submit.textContent = "Build my program list";
  }
});

$("refine").addEventListener("submit", (event) => {
  event.preventDefault();
  const text = $("refine-text").value.trim();
  if (!text) return;
  $("refine-text").value = "";
  followUp("messages", {
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
});

$("find-more").addEventListener("click", () => followUp("find-more"));

$("new-search").addEventListener("click", () => {
  if (source) source.close();
  remember(null);
  searchId = null;
  items = [];
  note = "";
  results.hidden = true;
  form.hidden = false;
  form.reset();
  form.scrollIntoView();
});

async function resume() {
  const saved = recall();
  if (!saved) return;
  try {
    const body = await api(`/api/searches/${encodeURIComponent(saved)}`);
    items = body.items;
    open(saved);
    setStage(items.length ? "done" : "searching");
    render();
    listen(saved);
  } catch {
    remember(null);
  }
}

resume();
