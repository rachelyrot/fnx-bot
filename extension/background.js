// Relays chat requests from the page to the bot server.
// Fetching here (not in the content script) avoids the page's CORS and
// local-network restrictions: the extension holds host permission for the server.
// Local development: switch to "http://127.0.0.1:8000" (also allowed in manifest.json).
const SERVER = "https://fnx-bot.onrender.com";

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "fnx-chat") return;
  const ctrl = new AbortController();
  port.onDisconnect.addListener(() => ctrl.abort());

  port.onMessage.addListener(async (req) => {
    try {
      const resp = await fetch(`${SERVER}/api/chat/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(req),
        signal: ctrl.signal,
      });
      if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split("\n\n");
        buffer = events.pop(); // keep a partial event for the next chunk
        for (const raw of events) {
          if (raw.startsWith("data: ")) port.postMessage(JSON.parse(raw.slice(6)));
        }
      }
    } catch (err) {
      if (!ctrl.signal.aborted) port.postMessage({ type: "error", message: String(err) });
    }
  });
});
