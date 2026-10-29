// OTHVM — Guard: runs every 3 minutes as SYSTEM via Scheduled Task "OTHVM Guard".
// 1) Telegram webhook: keeps allowed_updates = message + edited_message + callback_query (self-heal).
// 2) Queue hygiene: dedupe unclaimed queue rows by platform message id (edits supersede the old copy);
//    drop unclaimed rows whose Telegram message was deleted before it got executed.
// 3) Checkpoint hygiene: expire stale non-expired checkpoints (>5 min, no running execution) — this is the exact
//    bug class that froze the queue on 2026-10-18 (a suspended approval checkpoint blocking all processing).
// 4) Appends to guard.log (rotates at 1MB) and notifies the founder on Telegram ONLY when something actually changed.
const fs = require('fs');
const path = require('path');
const { DatabaseSync } = require('node:sqlite');

const CONFIG_FILE = path.join(__dirname, 'config.json');
if (!fs.existsSync(CONFIG_FILE)) {
  console.error('OTHVM Guard: missing config.json (copy config.example.json and fill it in).');
  process.exit(1);
}
const CFG = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'));
const DB = String(CFG.n8n_db_path || '');
const LOG = path.join(__dirname, 'guard.log');
const TG = String(CFG.telegram_bot_token || '');
const SECRET = String(CFG.telegram_secret || '');
const CHAT = String(CFG.telegram_chat_id || '');
const THREAD_LIKE = String(CFG.thread_like || '');
const STALE_MS = 5 * 60 * 1000;
const PROBE_AGE_MS = 90 * 1000;
if (!DB || !TG || !SECRET || !CHAT || !THREAD_LIKE) {
  console.error('OTHVM Guard: config.json is missing required values.');
  process.exit(1);
}

function rot() { try { const st = fs.statSync(LOG); if (st.size > 1024 * 1024) fs.rmSync(LOG, { force: true }); } catch (e) { } }
function log(o) { rot(); try { fs.appendFileSync(LOG, JSON.stringify(o) + '\n', 'utf8'); } catch (e) { } }
async function tg(method, body) {
  try {
    const r = await fetch('https://api.telegram.org/bot' + TG + '/' + method, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    return await r.json();
  } catch (e) { return { ok: false, error: String(e.message).slice(0, 120) }; }
}
async function probe(chat, mid) {
  try {
    const r = await fetch('https://api.telegram.org/bot' + TG + '/setMessageReaction', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ chat_id: String(chat), message_id: Number(mid), reaction: [] }) });
    const j = await r.json();
    if (j.ok === true) return 'exists';
    const d = String(j.description || '');
    if (/message to react not found|message not found/i.test(d)) return 'deleted';
    return 'unknown';
  } catch (e) { return 'unknown'; }
}
const nowFmt = () => new Date().toISOString().replace('T', ' ').slice(0, 23);

(async () => {
  const out = { at: new Date().toISOString(), acts: [] };

  // 1) webhook subscription self-heal
  try {
    const info = await fetch('https://api.telegram.org/bot' + TG + '/getWebhookInfo').then((r) => r.json());
    const au = (info.result && info.result.allowed_updates) || [];
    if (info.result && info.result.url && (!au.includes('edited_message') || !au.includes('callback_query'))) {
      const p = new URLSearchParams();
      p.set('url', info.result.url);
      p.set('secret_token', SECRET);
      p.set('allowed_updates', JSON.stringify(['message', 'edited_message', 'callback_query']));
      p.set('drop_pending_updates', 'false');
      const s = await fetch('https://api.telegram.org/bot' + TG + '/setWebhook', { method: 'POST', body: p }).then((r) => r.json());
      out.acts.push({ webhookResub: !!s.ok });
    }
  } catch (e) { out.acts.push({ webhookErr: String(e.message).slice(0, 120) }); }

  // 2) queue hygiene
  let db = null;
  try {
    db = new DatabaseSync(DB);
    db.exec('PRAGMA busy_timeout=8000');
    const rows = db.prepare("SELECT id, payload, executionId, createdAt FROM agent_message_queue WHERE threadId LIKE ? AND executionId IS NULL ORDER BY createdAt").all(THREAD_LIKE);
    const groups = new Map();
    for (const r of rows) {
      let pmid = '';
      try { const p = JSON.parse(r.payload); pmid = String((p.messageContext || {}).messageId || ''); } catch (e) { }
      if (!pmid) continue;
      if (!groups.has(pmid)) groups.set(pmid, []);
      groups.get(pmid).push(r);
    }
    for (const [pmid, list] of groups) {
      if (list.length > 1) {
        for (const r of list.slice(0, list.length - 1)) {
          db.prepare('DELETE FROM agent_message_queue WHERE id=? AND executionId IS NULL').run(r.id);
          out.acts.push({ dedupeDrop: pmid, row: r.id });
        }
      }
      const last = list[list.length - 1];
      const age = Date.now() - Date.parse(String(last.createdAt).replace(' ', 'T') + 'Z');
      if (!(age > PROBE_AGE_MS)) continue;
      let chat = '';
      let mid = '';
      try {
        const p = JSON.parse(last.payload);
        const pm = (p.messageContext || {}).platformMessage || {};
        chat = String(pm.chat_id || '');
        mid = String(pm.message_id || '');
        if (!chat || !mid) {
          const sp = String((p.messageContext || {}).messageId || '').split(':');
          if (sp.length >= 2) { chat = chat || sp[0]; mid = mid || sp[1]; }
        }
      } catch (e) { }
      if (chat && mid && /^\d+$/.test(mid)) {
        const st = await probe(chat, mid);
        if (st === 'deleted') {
          db.prepare('DELETE FROM agent_message_queue WHERE id=? AND executionId IS NULL').run(last.id);
          out.acts.push({ deletedDrop: pmid, row: last.id });
        }
      }
    }

    // 3) checkpoint hygiene
    const cps = db.prepare('SELECT rowid, threadId, runId, updatedAt FROM agent_checkpoints WHERE threadId LIKE ? AND expired=0').all(THREAD_LIKE);
    for (const c of cps) {
      const age = Date.now() - Date.parse(String(c.updatedAt).replace(' ', 'T') + 'Z');
      if (age <= STALE_MS) continue;
      const run = db.prepare("SELECT COUNT(*) n FROM agent_execution WHERE threadId=? AND status='running'").get(c.threadId);
      if (run && run.n > 0) continue;
      db.prepare('UPDATE agent_checkpoints SET expired=1, state=NULL, updatedAt=? WHERE rowid=?').run(nowFmt(), c.rowid);
      out.acts.push({ expiredCheckpoint: c.threadId, run: c.runId, staleMin: Math.round(age / 60000) });
    }
  } catch (e) {
    out.acts.push({ dbErr: String(e.message).slice(0, 160) });
  }
  try { if (db) db.close(); } catch (e) { }

  // 4) notify only on real changes
  const drops = out.acts.filter((a) => a.deletedDrop || a.dedupeDrop);
  const cpx = out.acts.filter((a) => a.expiredCheckpoint);
  if (drops.length || cpx.length) {
    const lines = [];
    if (drops.length) lines.push('🧹 دفاع تلقائي: شيلت ' + drops.length + ' عنصر من طابور التنفيذ — رسالة كانت اتمسحت/اتعدلت قبل ما تتنفذ.');
    if (cpx.length) lines.push('🧷 دفاع تلقائي: لقيت ' + cpx.length + ' تعليق قديم عالق كان ممكن يوقف الشغل — اتشال تلقائيًا.');
    const s = await tg('sendMessage', { chat_id: CHAT, text: lines.join('\n') });
    out.notified = !!(s && s.ok);
  }

  log(out);
})();
