// "Ask the site" widget, injected into fnx.co.il pages.
// Lives in a shadow root so the site's CSS and ours never touch.
// Its signature move: every answer can point at the exact sentence on the live site
// it came from, scrolling to it and highlighting it (on this page or after opening another).
(() => {
  if (document.getElementById("fnx-bot-host")) return;

  const STORE_KEY = "fnx-bot-ext-v1"; // sessionStorage of fnx.co.il: survives moving between pages in the tab
  const PENDING_KEY = "fnx-bot-ext-pending";
  const REFUSAL_ROUTES = new Set(["no_info", "advice", "off_topic", "empty", "too_long"]);
  const HIGHLIGHT = "fnx-bot-quote";

  // --- State -------------------------------------------------------------------
  let state = { history: [], rendered: [], open: false };
  try {
    const saved = JSON.parse(sessionStorage.getItem(STORE_KEY) || "null");
    if (saved && Array.isArray(saved.history) && Array.isArray(saved.rendered)) state = saved;
  } catch { /* storage blocked: start fresh */ }
  const save = () => {
    try { sessionStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch { /* ignore */ }
  };

  // --- Page context ------------------------------------------------------------
  const pageTitle = (document.title.split(/\s[|–-]\s/)[0] || "").trim();
  const samePage = (url) => {
    const norm = (u) => { const x = new URL(u, location.href); return (x.origin + x.pathname).replace(/\/$/, ""); };
    try { return norm(url) === norm(location.href); } catch { return false; }
  };

  function pageSuggestions() {
    const t = pageTitle;
    if (/^ביטוח/.test(t) && t.length < 40) {
      return [`מה כולל ${t}?`, `איך מגישים תביעה ב${t}?`, `איך מבטלים ${t}?`];
    }
    return ["איך מגישים תביעה?", "מה מספר הטלפון של שירות הלקוחות?", "יש לי נזילה בבית, איך מגישים תביעה?"];
  }

  // --- DOM ---------------------------------------------------------------------
  const host = document.createElement("div");
  host.id = "fnx-bot-host";
  host.style.cssText = "all: initial;"; // launcher and panel are position: fixed inside the shadow root
  document.documentElement.appendChild(host);
  const root = host.attachShadow({ mode: "open" });

  const logo = chrome.runtime.getURL("fnx-logo.png");
  root.innerHTML = `
    <link rel="stylesheet" href="${chrome.runtime.getURL("widget.css")}">
    <button class="launcher" type="button" aria-label="שאלו את האתר">
      <span class="launcher-dot"></span><span>שאלו את האתר</span>
    </button>
    <section class="panel" role="dialog" aria-label="שאלו את האתר" hidden>
      <header class="head">
        <img src="${logo}" alt="הפניקס">
        <div class="head-text">
          <strong>שאלו את האתר</strong>
          <span>כל תשובה עם הוכחה מהעמוד עצמו</span>
        </div>
        <button class="icon new" type="button" title="שיחה חדשה" aria-label="שיחה חדשה">
          <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M12 5v14M5 12h14" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>
        </button>
        <button class="icon close" type="button" title="סגירה" aria-label="סגירה">
          <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>
        </button>
      </header>
      <div class="context"></div>
      <div class="messages" aria-live="polite"></div>
      <div class="chips"></div>
      <form class="composer" autocomplete="off">
        <input type="text" maxlength="1000" placeholder="שאלה על העמוד או על כל האתר..." aria-label="שאלה">
        <button class="send" type="submit" aria-label="שליחה">
          <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M19 12H5M11 6l-6 6 6 6" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
        </button>
      </form>
    </section>`;

  const $ = (sel) => root.querySelector(sel);
  const launcher = $(".launcher"), panel = $(".panel"), messagesEl = $(".messages");
  const chipsEl = $(".chips"), form = $(".composer"), input = $(".composer input"), sendBtn = $(".send");

  $(".context").innerHTML = pageTitle
    ? `<span class="pin"></span>את/ה בעמוד: <b>${escapeHtml(pageTitle)}</b>`
    : "";

  function renderChips() {
    chipsEl.innerHTML = pageSuggestions()
      .map((q) => `<button type="button" class="chip" data-q="${escapeHtml(q)}">${escapeHtml(q)}</button>`)
      .join("");
    chipsEl.hidden = state.rendered.length > 0;
  }

  function setOpen(open) {
    state.open = open;
    save();
    panel.hidden = !open;
    launcher.hidden = open;
    if (open) { input.focus(); messagesEl.scrollTop = messagesEl.scrollHeight; }
  }

  // --- Formatting (same rules as the web UI) ------------------------------------
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function formatAnswer(text) {
    const lines = escapeHtml(text.trim()).split(/\n/);
    let html = "", list = null;
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
      .replace(/(https?:\/\/[^\s<]+[^\s<.,)])/g, '<a href="$1">$1</a>');
  }

  function sourcesHtml(sources) {
    if (!sources || !sources.length) return "";
    const rows = sources.map((s) => {
      const here = samePage(s.url);
      const show = s.quote
        ? `<button type="button" class="show" data-url="${escapeHtml(s.url)}" data-quote="${escapeHtml(s.quote)}">
             <span class="pin"></span>${here ? "הראו לי כאן בעמוד" : "הראו לי בעמוד"}</button>`
        : "";
      return `<div class="source">
          <a href="${escapeHtml(s.url)}">${escapeHtml(s.title || s.url)}</a>${here ? '<span class="here">העמוד הזה</span>' : ""}
          ${show}
        </div>`;
    }).join("");
    return `<div class="sources"><div class="sources-title">המקורות באתר</div>${rows}</div>`;
  }

  function addMessage(role, html, cls = "", persist = true) {
    if (persist) { state.rendered.push({ role, html, cls }); save(); }
    const wrap = document.createElement("div");
    wrap.className = `msg ${role} ${cls}`.trim();
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.innerHTML = html;
    wrap.appendChild(bubble);
    messagesEl.appendChild(wrap);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return wrap;
  }

  // --- Asking -------------------------------------------------------------------
  function ask(question) {
    question = question.trim();
    if (!question || sendBtn.disabled) return;
    chipsEl.hidden = true;
    addMessage("user", escapeHtml(question));
    input.value = "";
    sendBtn.disabled = true;
    const typing = addMessage("bot", "<span></span><span></span><span></span>", "typing", false);
    let live = null, streamed = "";

    const port = chrome.runtime.connect({ name: "fnx-chat" });
    const finish = () => { sendBtn.disabled = false; input.focus(); try { port.disconnect(); } catch { /* gone */ } };
    const fail = (why) => {
      typing.remove();
      if (live) live.remove();
      addMessage("bot", "משהו השתבש בדרך. אפשר לנסות שוב בעוד רגע.", "refusal", false);
      console.warn("[fnx-bot]", why);
      finish();
    };

    port.onMessage.addListener((ev) => {
      if (ev.type === "token") {
        streamed += ev.delta;
        if (!live) { typing.remove(); live = addMessage("bot", "", "", false); }
        live.querySelector(".bubble").innerHTML = formatAnswer(streamed);
        messagesEl.scrollTop = messagesEl.scrollHeight;
      } else if (ev.type === "final") {
        // the verified answer (citations checked) replaces the streamed draft
        typing.remove();
        if (live) live.remove();
        addMessage("bot", formatAnswer(ev.text) + sourcesHtml(ev.sources), REFUSAL_ROUTES.has(ev.route) ? "refusal" : "");
        state.history.push({ role: "user", content: question }, { role: "assistant", content: ev.text });
        state.history = state.history.slice(-8);
        save();
        finish();
        // the answer comes from the page being read: show the sentence right away
        const here = (ev.sources || []).find((s) => s.quote && samePage(s.url));
        if (here) setTimeout(() => showQuote(here.quote), 400);
      } else if (ev.type === "error") {
        fail(ev.message);
      }
    });
    port.onDisconnect.addListener(() => { if (sendBtn.disabled) fail("connection closed"); });
    port.postMessage({ message: question, history: state.history, page: pageTitle });
  }

  // --- Show me on the page --------------------------------------------------------
  function textNodes() {
    const nodes = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => (n.parentElement && !n.parentElement.closest("script,style,noscript") && n.nodeValue.trim()
        ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT),
    });
    while (walker.nextNode()) nodes.push(walker.currentNode);
    return nodes;
  }

  // Find the quote in the live page text, tolerant to whitespace and markup splits.
  function findRange(quote) {
    const nodes = textNodes();
    const starts = [];
    let full = "";
    for (const n of nodes) { starts.push(full.length); full += n.nodeValue; }
    const esc = (w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const words = quote.split(/\s+/).filter(Boolean);
    const attempts = [words, words.slice(0, 8)]; // whole sentence, else its opening
    for (const ws of attempts) {
      if (ws.length < 3) continue;
      const m = new RegExp(ws.map(esc).join("\\s*")).exec(full);
      if (!m) continue;
      const at = (pos) => {
        let i = starts.length - 1;
        while (i > 0 && starts[i] > pos) i--;
        return [nodes[i], pos - starts[i]];
      };
      const [sn, so] = at(m.index);
      const [en, eo] = at(m.index + m[0].length - 1);
      const range = document.createRange();
      range.setStart(sn, so);
      range.setEnd(en, eo + 1);
      return range;
    }
    return null;
  }

  function ensureHighlightStyle() {
    if (document.getElementById("fnx-bot-style")) return;
    const style = document.createElement("style");
    style.id = "fnx-bot-style";
    style.textContent = `
      ::highlight(${HIGHLIGHT}) { background-color: #ffe27a; color: #111c3f; }
      .fnx-bot-ring { position: absolute; pointer-events: none; z-index: 2147482999;
        border: 3px solid #ff4e31; border-radius: 10px; box-shadow: 0 0 0 6px rgba(255,78,49,.18);
        animation: fnx-bot-pulse 1.1s ease-out 3; transition: opacity .6s; }
      @keyframes fnx-bot-pulse { 0% { box-shadow: 0 0 0 0 rgba(255,78,49,.45); } 100% { box-shadow: 0 0 0 16px rgba(255,78,49,0); } }`;
    document.head.appendChild(style);
  }

  function showQuote(quote) {
    const range = findRange(quote);
    if (!range) { toast("לא מצאתי את המשפט בעמוד הזה — אפשר לפתוח את המקור מהקישור."); return false; }
    const el = range.startContainer.parentElement;
    el.closest("details")?.setAttribute("open", "");
    if (!el.getClientRects().length) {
      toast("המשפט נמצא בחלק מקופל בעמוד — פתחו את השאלות הנפוצות כדי לראות אותו.");
      return false;
    }
    ensureHighlightStyle();
    if (window.CSS && CSS.highlights && window.Highlight) {
      CSS.highlights.set(HIGHLIGHT, new Highlight(range));
    } else {
      const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
    }
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    setTimeout(() => ring(range), 650);
    return true;
  }

  function ring(range) {
    document.querySelectorAll(".fnx-bot-ring").forEach((r) => r.remove());
    const rects = [...range.getClientRects()].filter((r) => r.width && r.height);
    if (!rects.length) return;
    const top = Math.min(...rects.map((r) => r.top)), bottom = Math.max(...rects.map((r) => r.bottom));
    const left = Math.min(...rects.map((r) => r.left)), right = Math.max(...rects.map((r) => r.right));
    const box = document.createElement("div");
    box.className = "fnx-bot-ring";
    Object.assign(box.style, {
      top: `${top + scrollY - 6}px`, left: `${left + scrollX - 8}px`,
      width: `${right - left + 16}px`, height: `${bottom - top + 12}px`,
    });
    document.body.appendChild(box);
    setTimeout(() => { box.style.opacity = "0"; setTimeout(() => box.remove(), 700); }, 4200);
  }

  function toast(text) {
    const t = document.createElement("div");
    t.className = "toast";
    t.textContent = text;
    panel.appendChild(t);
    setTimeout(() => t.remove(), 4000);
  }

  // Text fragment: Chrome highlights it on arrival even before this script loads.
  function withTextFragment(url, quote) {
    const enc = (s) => encodeURIComponent(s).replace(/-/g, "%2D").replace(/,/g, "%2C");
    const w = quote.split(/\s+/).filter(Boolean);
    const frag = w.length <= 10 ? enc(w.join(" ")) : `${enc(w.slice(0, 5).join(" "))},${enc(w.slice(-5).join(" "))}`;
    return `${url.split("#")[0]}#:~:text=${frag}`;
  }

  // --- Events -------------------------------------------------------------------
  launcher.addEventListener("click", () => setOpen(true));
  $(".close").addEventListener("click", () => setOpen(false));
  $(".new").addEventListener("click", () => {
    state = { history: [], rendered: [], open: true };
    save();
    messagesEl.innerHTML = "";
    greet();
    renderChips();
    CSS.highlights?.delete(HIGHLIGHT);
    input.focus();
  });
  form.addEventListener("submit", (e) => { e.preventDefault(); ask(input.value); });
  chipsEl.addEventListener("click", (e) => { const c = e.target.closest(".chip"); if (c) ask(c.dataset.q); });
  messagesEl.addEventListener("click", (e) => {
    const btn = e.target.closest(".show");
    if (!btn) return;
    const { url, quote } = btn.dataset;
    if (samePage(url)) { showQuote(quote); return; }
    try { sessionStorage.setItem(PENDING_KEY, JSON.stringify({ url, quote })); } catch { /* ignore */ }
    location.href = withTextFragment(url, quote);
  });
  // keep site keyboard shortcuts from stealing typing inside the widget
  for (const type of ["keydown", "keyup", "keypress"]) host.addEventListener(type, (e) => e.stopPropagation());

  // --- Start ----------------------------------------------------------------------
  function greet() {
    addMessage("bot", `שלום! אני עונה מתוך אתר הפניקס בלבד, ומראה לך בדיוק איפה זה כתוב.
      <br>אפשר לשאול גם על העמוד הזה — למשל "מה זה כולל?"`, "", false);
  }
  greet();
  for (const m of state.rendered) addMessage(m.role, m.html, m.cls, false);
  renderChips();
  setOpen(state.open);

  // arrived from "show me on the page": highlight the sentence we were sent to
  try {
    const pending = JSON.parse(sessionStorage.getItem(PENDING_KEY) || "null");
    sessionStorage.removeItem(PENDING_KEY);
    if (pending && samePage(pending.url)) setTimeout(() => showQuote(pending.quote), 900);
  } catch { /* ignore */ }
})();
