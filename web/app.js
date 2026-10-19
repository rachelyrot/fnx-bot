const messagesEl = document.getElementById("messages");
const form = document.getElementById("composer");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");
const suggestions = document.getElementById("suggestions");

const newChatBtn = document.getElementById("new-chat");

const REFUSAL_ROUTES = new Set(["no_info", "advice", "off_topic", "empty", "too_long"]);
const STORE_KEY = "fnx-chat-v1";

// Conversation survives a page refresh (until the tab is closed).
// history: [{role, content}] sent to the server for follow-up questions.
// rendered: [{role, html, cls}] to redraw the bubbles.
let state = { history: [], rendered: [] };

function save() {
  try { sessionStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch { /* storage unavailable */ }
}

function restore() {
  try {
    const saved = JSON.parse(sessionStorage.getItem(STORE_KEY) || "null");
    if (saved && Array.isArray(saved.history) && Array.isArray(saved.rendered)) state = saved;
  } catch { /* ignore broken or blocked storage */ }
  for (const m of state.rendered) addMessage(m.role, m.html, m.cls, false);
  if (state.rendered.length) suggestions.hidden = true;
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Minimal, safe formatting: escape first, then paragraphs, bullet lists, bold and links.
function formatAnswer(text) {
  const lines = escapeHtml(text.trim()).split(/\n/);
  let html = "";
  let list = null;
  for (const raw of lines) {
    const line = raw.trim();
    const bullet = line.match(/^(?:[-*•]|\d+[.)])\s+(.*)$/);
    if (bullet) {
      const tag = /^\d/.test(line) ? "ol" : "ul";
      if (list !== tag) { if (list) html += `</${list}>`; html += `<${tag}>`; list = tag; }
      html += `<li>${bullet[1]}</li>`;
      continue;
    }
    if (list) { html += `</${list}>`; list = null; }
    if (line) html += `<p>${line}</p>`;
  }
  if (list) html += `</${list}>`;
  return html
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(https?:\/\/[^\s<]+[^\s<.,)])/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
}

function addMessage(role, html, extraClass = "", persist = true) {
  if (persist) {
    state.rendered.push({ role, html, cls: extraClass });
    save();
  }
  const wrap = document.createElement("div");
  wrap.className = `msg ${role} ${extraClass}`.trim();
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.innerHTML = html;
  wrap.appendChild(bubble);
  messagesEl.appendChild(wrap);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return wrap;
}

// Text fragment (#:~:text=): the browser opens the page scrolled to the quoted sentence, highlighted.
function withTextFragment(url, quote) {
  if (!quote) return url;
  const enc = (s) => encodeURIComponent(s).replace(/-/g, "%2D").replace(/,/g, "%2C");
  const w = quote.split(/\s+/).filter(Boolean);
  const frag = w.length <= 10 ? enc(w.join(" ")) : `${enc(w.slice(0, 5).join(" "))},${enc(w.slice(-5).join(" "))}`;
  return `${url.split("#")[0]}#:~:text=${frag}`;
}

function sourcesHtml(sources) {
  if (!sources || !sources.length) return "";
  const links = sources
    .map((s) => `<a class="source" href="${escapeHtml(withTextFragment(s.url, s.quote))}" target="_blank" rel="noopener"
      ${s.quote ? `title="${escapeHtml(s.quote)}"` : ""}>${escapeHtml(s.title || s.url)}</a>`)
    .join("");
  return `<div class="sources"><div class="sources-title">עמודים שנבדקו באתר</div>${links}</div>`;
}

async function ask(question) {
  question = question.trim();
  if (!question) return;
  suggestions.hidden = true;
  addMessage("user", escapeHtml(question));
  input.value = "";
  sendBtn.disabled = true;
  const typing = addMessage("bot", "<span></span><span></span><span></span>", "typing", false);

  let live = null; // bubble that fills up while tokens stream in
  try {
    const resp = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: question, history: state.history }),
    });
    if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let streamed = "";
    let final = null;

    while (!final) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split("\n\n");
      buffer = events.pop(); // keep a partial event for the next chunk
      for (const raw of events) {
        if (!raw.startsWith("data: ")) continue;
        const ev = JSON.parse(raw.slice(6));
        if (ev.type === "token") {
          streamed += ev.delta;
          if (!live) { typing.remove(); live = addMessage("bot", "", "", false); }
          live.querySelector(".bubble").innerHTML = formatAnswer(streamed);
          messagesEl.scrollTop = messagesEl.scrollHeight;
        } else if (ev.type === "final") {
          final = ev;
        } else if (ev.type === "error") {
          throw new Error(ev.message);
        }
      }
    }
    if (!final) throw new Error("stream ended without an answer");

    // Replace the streamed draft with the verified answer (citations checked) + sources.
    typing.remove();
    if (live) live.remove();
    const refusal = REFUSAL_ROUTES.has(final.route) ? "refusal" : "";
    addMessage("bot", formatAnswer(final.text) + sourcesHtml(final.sources), refusal);
    state.history.push({ role: "user", content: question }, { role: "assistant", content: final.text });
    save();
  } catch (err) {
    typing.remove();
    if (live) live.remove();
    addMessage("bot", "משהו השתבש בדרך. אפשר לנסות שוב בעוד רגע.", "refusal", false);
    console.error(err);
  } finally {
    sendBtn.disabled = false;
    input.focus();
  }
}

// Wake the server (it sleeps when idle) while the visitor is still reading or typing.
fetch("/api/health").catch(() => {});

form.addEventListener("submit", (e) => { e.preventDefault(); ask(input.value); });
suggestions.addEventListener("click", (e) => {
  const card = e.target.closest(".card");
  if (card) ask(card.dataset.q);
});

newChatBtn.addEventListener("click", () => {
  state = { history: [], rendered: [] };
  save();
  messagesEl.querySelectorAll(".msg:not(.welcome)").forEach((el) => el.remove());
  suggestions.hidden = false;
  input.value = "";
  input.focus();
});

restore();
input.focus();
