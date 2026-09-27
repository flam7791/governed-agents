"""The approvals page: one small HTML page, served by the API, for the people who decide.

Everything an agent proposed is shown as text, never as HTML: arguments can carry content that
came from documents, including injected markup, so the page only ever sets textContent.
"""

APPROVALS_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent approvals</title>
<style>
  :root {
    --bg: #f6f7f9; --panel: #ffffff; --text: #1d2330; --muted: #5d6576; --line: #dde1e8;
    --accent: #1f5fbf; --ok: #1d7a46; --bad: #b3261e; --warn: #9a6200; --code: #f0f2f5;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #14171c; --panel: #1c2027; --text: #e6e9ef; --muted: #9aa3b2; --line: #2d333d;
      --accent: #7aa7ff; --ok: #5cc98a; --bad: #ff8a80; --warn: #e2b04a; --code: #232830;
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
  main { max-width: 960px; margin: 0 auto; padding: 24px 16px 48px; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  h2 { font-size: 16px; margin: 28px 0 10px; }
  .muted { color: var(--muted); }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
           padding: 16px; margin-bottom: 12px; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  input, textarea { font: inherit; color: var(--text); background: var(--panel);
                    border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; }
  input { flex: 1; min-width: 200px; }
  textarea { width: 100%; min-height: 56px; margin: 8px 0; }
  button { font: inherit; border: 0; border-radius: 8px; padding: 8px 14px; cursor: pointer;
           color: #fff; background: var(--accent); }
  button.ok { background: var(--ok); } button.bad { background: var(--bad); }
  button:disabled { opacity: .5; cursor: default; }
  pre { background: var(--code); border-radius: 8px; padding: 10px; overflow-x: auto;
        white-space: pre-wrap; word-break: break-word; margin: 8px 0; font-size: 13px; }
  .tag { display: inline-block; font-size: 12px; padding: 1px 8px; border-radius: 99px;
         border: 1px solid var(--line); color: var(--muted); }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); }
  tr.clickable { cursor: pointer; } tr.clickable:hover { background: var(--code); }
  .status-completed { color: var(--ok); } .status-failed, .status-halted { color: var(--bad); }
  .status-waiting_approval { color: var(--warn); }
  #message { min-height: 22px; }
  .table-wrap { overflow-x: auto; }
</style>
</head>
<body>
<main>
  <h1>Agent approvals</h1>
  <p class="muted">Actions that agents want to take and that need a person. Your decision is
  recorded under your name, and the agent is told the outcome.</p>

  <div class="panel row">
    <input id="token" type="password" placeholder="Your access token" autocomplete="off">
    <button id="signin">Sign in</button>
    <span id="who" class="muted"></span>
  </div>
  <div id="message" class="muted"></div>

  <h2>Waiting for a person</h2>
  <div id="approvals"><p class="muted">Sign in to see pending actions.</p></div>

  <h2>Recent runs</h2>
  <div class="panel table-wrap"><table>
    <thead><tr><th>Run</th><th>Scenario</th><th>Status</th><th>Requested by</th>
    <th>Cost</th></tr></thead>
    <tbody id="runs"></tbody>
  </table></div>
  <div id="trace"></div>
</main>
<script>
(() => {
  let token = "";
  let shown = null;
  try { token = sessionStorage.getItem("govagents-token") || ""; } catch (e) {}
  const $ = (id) => document.getElementById(id);
  const el = (tag, text, cls) => {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (cls) node.className = cls;
    return node;
  };
  const say = (text) => { $("message").textContent = text || ""; };

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Authorization": "Bearer " + token, "Content-Type": "application/json" },
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || ("HTTP " + response.status));
    return body;
  }

  async function signIn() {
    token = $("token").value.trim() || token;
    try {
      const me = await api("/api/me");
      $("who").textContent = "Signed in as " + me.name + " (" + me.role + ")";
      try { sessionStorage.setItem("govagents-token", token); } catch (e) {}
      $("token").value = "";
      refresh();
    } catch (e) { say("Sign-in failed: " + e.message); }
  }

  function approvalCard(a) {
    const card = el("div", null, "panel");
    const head = el("div", null, "row");
    head.append(el("strong", a.tool), el("span", "proposed by " + a.agent, "muted"),
                el("span", a.run_id, "tag"));
    const reason = el("div", "Why a person is needed: " + a.reason, "muted");
    const args = el("pre", JSON.stringify(a.arguments, null, 2));
    const note = el("textarea");
    note.placeholder = "Note (required to reject; the agent reads it)";
    const approve = el("button", "Approve", "ok");
    const reject = el("button", "Reject", "bad");
    const decide = async (verdict) => {
      approve.disabled = reject.disabled = true;
      try {
        await api("/api/approvals/" + encodeURIComponent(a.id) + "/" + verdict,
                  { method: "POST", body: JSON.stringify({ note: note.value }) });
        say((verdict === "approve" ? "Approved " : "Rejected ") + a.id + ". The run resumes.");
        setTimeout(refresh, 800);
      } catch (e) {
        say("Not recorded: " + e.message);
        approve.disabled = reject.disabled = false;
      }
    };
    approve.onclick = () => decide("approve");
    reject.onclick = () => decide("reject");
    const buttons = el("div", null, "row");
    buttons.append(approve, reject);
    card.append(head, reason, args, note, buttons);
    return card;
  }

  async function showTrace(runId) {
    try {
      const run = await api("/api/runs/" + encodeURIComponent(runId));
      const box = el("div", null, "panel");
      box.append(el("strong", "Audit trail of " + run.id),
                 el("div", run.status + " · " + run.spent_usd.toFixed(4) + " USD", "muted"));
      const lines = run.events.map((e) => {
        const when = new Date(e.ts * 1000).toLocaleTimeString();
        return when + "  " + (e.agent || "-").padEnd(14) + " " + e.kind.padEnd(18) + " " +
               JSON.stringify(e.detail).slice(0, 220);
      });
      box.append(el("pre", lines.join("\\n")));
      $("trace").replaceChildren(box);
    } catch (e) { say("Could not load the trail: " + e.message); }
  }

  async function refresh() {
    if (!token) return;
    try {
      const pending = await api("/api/approvals");
      const signature = pending.map((a) => a.id).join(",");
      if (signature !== shown) {  // re-draw only when the list changes, keeping typed notes
        shown = signature;
        $("approvals").replaceChildren(...(pending.length
          ? pending.map(approvalCard) : [el("p", "Nothing is waiting.", "muted")]));
      }
      const runs = await api("/api/runs");
      $("runs").replaceChildren(...runs.map((r) => {
        const tr = el("tr", null, "clickable");
        tr.append(el("td", r.id), el("td", r.scenario),
                  el("td", r.status, "status-" + r.status), el("td", r.requested_by || "-"),
                  el("td", (r.spent_usd || 0).toFixed(4) + " USD"));
        tr.onclick = () => showTrace(r.id);
        return tr;
      }));
    } catch (e) { say(e.message); }
  }

  $("signin").onclick = signIn;
  $("token").addEventListener("keydown", (e) => { if (e.key === "Enter") signIn(); });
  if (token) signIn();
  setInterval(() => { if (token) refresh(); }, 5000);
})();
</script>
</body>
</html>
"""
