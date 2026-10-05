// eFootball Champions League – zero-dependency Node server with a SQLite .db file.
// Requires Node 22.13+ (uses the built-in node:sqlite module).
const http = require('http'), fs = require('fs'), path = require('path'), crypto = require('crypto');
const net = require('net'), tls = require('tls'), os = require('os');
const { DatabaseSync } = require('node:sqlite');

// Settings can live in a .env file next to this one (variables set in the shell win over the file).
try { process.loadEnvFile(path.join(__dirname, '.env')); } catch (e) { if (e.code !== 'ENOENT') console.error('Could not read .env:', e.message); }

const E = process.env;
const PORT = +E.PORT || 3000;
const DIR = __dirname;
const TRUST_PROXY = E.TRUST_PROXY === '1';
const APP_URL = (E.APP_URL || '').replace(/\/+$/, '');
const SITE = 'eFootball Champions League';
const DAY = 864e5, SESSION_MS = 7 * DAY, INVITE_MS = 2 * DAY, RESET_MS = 3600e3;

const DB_FILE = E.DB_FILE || path.join(DIR, 'data', 'league.db');
fs.mkdirSync(path.dirname(DB_FILE), { recursive: true });
const db = new DatabaseSync(DB_FILE);
db.exec('PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;');
db.exec(`
CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1), json TEXT NOT NULL, updated TEXT);
CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  email TEXT UNIQUE COLLATE NOCASE, role TEXT NOT NULL DEFAULT 'admin', salt TEXT, hash TEXT,
  must_change INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL, invited_by TEXT, last_login INTEGER);
CREATE TABLE IF NOT EXISTS tokens(hash TEXT PRIMARY KEY, admin_id INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS draws(id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, by TEXT NOT NULL, kind TEXT NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, actor TEXT, action TEXT NOT NULL, ip TEXT);
`);
if (!db.prepare("SELECT 1 AS x FROM pragma_table_info('draws') WHERE name='role'").get()) db.exec('ALTER TABLE draws ADD COLUMN role TEXT');
// Old schema: sessions(token, expires) held raw tokens with no owner – replace it.
if (!db.prepare("SELECT 1 AS x FROM pragma_table_info('sessions') WHERE name='admin_id'").get()) {
  db.exec(`DROP TABLE IF EXISTS sessions;
  CREATE TABLE sessions(hash TEXT PRIMARY KEY, admin_id INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL, expires INTEGER NOT NULL, ip TEXT);`);
}
// Old schema: a single `admin` row. Move it over as the owner.
if (db.prepare("SELECT 1 AS x FROM sqlite_master WHERE name='admin'").get()) {
  const a = db.prepare('SELECT * FROM admin WHERE id=1').get();
  if (a && !db.prepare('SELECT 1 AS x FROM admins').get())
    db.prepare("INSERT INTO admins(username,role,salt,hash,created) VALUES(?,'owner',?,?,?)").run(a.username, a.salt, a.hash, Date.now());
  db.exec('DROP TABLE admin');
}

// ---------- backups: a snapshot on every start and once a day, newest 14 kept ----------
const BACKUP_DIR = E.BACKUP_DIR || path.join(path.dirname(DB_FILE), 'backups');
function backup() {
  try {
    fs.mkdirSync(BACKUP_DIR, { recursive: true });
    const f = path.join(BACKUP_DIR, `league-${new Date().toISOString().replace(/[:T]/g, '-').slice(0, 16)}.db`);
    if (fs.existsSync(f)) return;
    db.exec(`VACUUM INTO '${f.replace(/'/g, "''")}'`);
    const old = fs.readdirSync(BACKUP_DIR).filter(n => /^league-.*\.db$/.test(n)).sort().slice(0, -14);
    for (const n of old) fs.unlinkSync(path.join(BACKUP_DIR, n));
  } catch (e) { console.error('Backup failed:', e.message); }
}
backup(); setInterval(backup, DAY).unref();

// ---------- helpers ----------
const scrypt = (pw, salt) => new Promise((ok, no) => crypto.scrypt(pw, salt, 64, (e, k) => e ? no(e) : ok(k.toString('hex'))));
const sha = s => crypto.createHash('sha256').update(s).digest('hex');
const rnd = (n = 32) => crypto.randomBytes(n).toString('hex');
const USER_RE = /^[A-Za-z0-9_.-]{3,32}$/;
const EMAIL_RE = /^[^\s@<>()",;:]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,}$/;
function pwProblem(pw, user) {
  if (typeof pw !== 'string' || pw.length < 10) return 'Password must be at least 10 characters.';
  if (pw.length > 200) return 'Password is too long.';
  if (user && pw.toLowerCase().includes(user.toLowerCase())) return 'Password must not contain the username.';
  if (/^(.)\1+$/.test(pw) || /^(password|admin|qwerty|123456)/i.test(pw)) return 'Password is too easy to guess.';
  return null;
}
async function setPassword(id, pw) {
  const salt = rnd(16);
  db.prepare('UPDATE admins SET salt=?, hash=?, must_change=0 WHERE id=?').run(salt, await scrypt(pw, salt), id);
  db.prepare('DELETE FROM sessions WHERE admin_id=?').run(id);
  db.prepare('DELETE FROM tokens WHERE admin_id=?').run(id);
}
const audit = (actor, action, ip) => db.prepare('INSERT INTO audit(ts,actor,action,ip) VALUES(?,?,?,?)').run(Date.now(), actor || null, action, ip || null);
const pub = a => ({ id: a.id, username: a.username, email: a.email, role: a.role, pending: !a.hash, mustChange: !!a.must_change, lastLogin: a.last_login, created: a.created, invitedBy: a.invited_by });

// ---------- bootstrap login ----------
// The port only opens once this has finished, so the first login can't race the password setup.
const ready = (async () => {
  const weak = E.ADMIN_PASSWORD && pwProblem(E.ADMIN_PASSWORD, E.ADMIN_USER || 'admin');
  if (weak) console.error(`!! ADMIN_PASSWORD was NOT applied: ${weak} Fix it in .env and restart.`);
  if (E.ADMIN_PASSWORD && !weak) {
    const user = E.ADMIN_USER || 'admin';
    let a = db.prepare('SELECT * FROM admins WHERE username=?').get(user);
    if (!a) {
      const hasOwner = db.prepare("SELECT 1 AS x FROM admins WHERE role='owner'").get();
      db.prepare('INSERT INTO admins(username,role,created) VALUES(?,?,?)').run(user, hasOwner ? 'admin' : 'owner', Date.now());
      a = db.prepare('SELECT * FROM admins WHERE username=?').get(user);
    }
    if (E.ADMIN_EMAIL && EMAIL_RE.test(E.ADMIN_EMAIL)) db.prepare('UPDATE admins SET email=? WHERE id=?').run(E.ADMIN_EMAIL, a.id);
    // Only reset when it's really a new password, so leaving it in .env doesn't log the admin out on every restart.
    const same = a.hash && crypto.timingSafeEqual(Buffer.from(await scrypt(E.ADMIN_PASSWORD, a.salt), 'hex'), Buffer.from(a.hash, 'hex'));
    if (!same) {
      await setPassword(a.id, E.ADMIN_PASSWORD);
      audit('server', `password for ${user} set from ADMIN_PASSWORD`);
      console.log(`Password for "${user}" was set from ADMIN_PASSWORD. Remove it from .env once you can log in.`);
    }
  } else if (!db.prepare('SELECT 1 AS x FROM admins').get()) {
    const pw = rnd(6);
    db.prepare("INSERT INTO admins(username,role,created) VALUES('admin','owner',?)").run(Date.now());
    const id = db.prepare("SELECT id FROM admins WHERE username='admin'").get().id;
    await setPassword(id, pw);
    db.prepare('UPDATE admins SET must_change=1 WHERE id=?').run(id);
    console.warn(`!! First run. Log in as  admin / ${pw}  – you will be asked to choose a new password.`);
  }
})().catch(e => { console.error('Could not set up the admin login:', e); process.exit(1); });

// ---------- rate limiting ----------
const buckets = new Map();
function hit(key, limit, windowMs) {             // records an event, returns true when over the limit
  const now = Date.now(), a = (buckets.get(key) || []).filter(t => now - t < windowMs);
  a.push(now); buckets.set(key, a); return a.length > limit;
}
function over(key, limit, windowMs) {            // checks without recording
  const now = Date.now(); return (buckets.get(key) || []).filter(t => now - t < windowMs).length >= limit;
}
setInterval(() => {
  const now = Date.now();
  for (const [k, a] of buckets) if (!a.length || now - a[a.length - 1] > DAY) buckets.delete(k);
  db.prepare('DELETE FROM sessions WHERE expires<?').run(now);
  db.prepare('DELETE FROM tokens WHERE expires<?').run(now);
  db.prepare('DELETE FROM audit WHERE ts<?').run(now - 90 * DAY);
}, 600e3).unref();

// ---------- SMTP (no dependencies) ----------
const SMTP = { host: E.SMTP_HOST, port: +E.SMTP_PORT || 587, user: E.SMTP_USER, pass: E.SMTP_PASS,
  from: E.SMTP_FROM || E.SMTP_USER, secure: E.SMTP_SECURE ? E.SMTP_SECURE === '1' : +E.SMTP_PORT === 465 };
const smtpReady = () => !!(SMTP.host && SMTP.from);
const clean = s => String(s).replace(/[\r\n]+/g, ' ').trim();
const encWord = s => /^[\x20-\x7e]*$/.test(s) ? s : `=?UTF-8?B?${Buffer.from(s).toString('base64')}?=`;
const b64 = s => Buffer.from(s).toString('base64').replace(/.{76}/g, '$&\r\n');
const addr = s => (String(s).match(/<([^>]+)>/) || [0, s])[1].trim();

function sendMail({ to, subject, text, html }) {
  return new Promise((resolve, reject) => {
    if (!smtpReady()) return reject(new Error('SMTP is not configured'));
    if (!EMAIL_RE.test(to)) return reject(new Error('Bad recipient'));
    let sock, buf = '', lines = [], waiter = null, queued = [], done = false;
    const fail = e => { if (done) return; if (!e.message) e.message = e.code || (e.errors && e.errors[0] && e.errors[0].message) || 'connection failed'; done = true; try { sock.destroy(); } catch {} reject(e); };
    const timer = setTimeout(() => fail(new Error('SMTP timeout')), 20000);
    const onData = d => {
      buf += d;
      let i; while ((i = buf.indexOf('\n')) >= 0) {
        const l = buf.slice(0, i).replace(/\r$/, ''); buf = buf.slice(i + 1); lines.push(l);
        if (/^\d{3}(?: |$)/.test(l)) { const r = { code: +l.slice(0, 3), text: lines.join('\n') }; lines = []; waiter ? (waiter(r), waiter = null) : queued.push(r); }
      }
    };
    const attach = s => { sock = s; s.setEncoding('utf8'); s.on('data', onData); s.on('error', fail); };
    const read = () => new Promise(ok => queued.length ? ok(queued.shift()) : (waiter = ok));
    const cmd = async (line, want, secret) => {
      if (line != null) sock.write(line + '\r\n');
      const r = await read();
      if (!want.includes(r.code)) throw new Error(`SMTP ${secret ? '(auth)' : line ? line.split(' ')[0] : 'greeting'}: ${r.text}`);
      return r;
    };
    const opts = { host: SMTP.host, port: SMTP.port, servername: SMTP.host };
    attach(SMTP.secure ? tls.connect(opts) : net.connect(opts));
    (async () => {
      const me = clean(os.hostname()).replace(/[^A-Za-z0-9.-]/g, '') || 'localhost';
      await cmd(null, [220]);
      let ehlo = await cmd(`EHLO ${me}`, [250]);
      if (!SMTP.secure) {
        if (!/STARTTLS/i.test(ehlo.text)) throw new Error('Server does not offer STARTTLS; refusing to send credentials in plain text');
        await cmd('STARTTLS', [220]);
        sock.removeListener('data', onData);
        await new Promise((ok, no) => { const t = tls.connect({ socket: sock, servername: SMTP.host }, ok); t.on('error', no); attach(t); });
        ehlo = await cmd(`EHLO ${me}`, [250]);
      }
      if (SMTP.user) {
        if (/AUTH[ =][^\n]*PLAIN/i.test(ehlo.text)) await cmd('AUTH PLAIN ' + Buffer.from(`\0${SMTP.user}\0${SMTP.pass || ''}`).toString('base64'), [235], 1);
        else {
          await cmd('AUTH LOGIN', [334]);
          await cmd(Buffer.from(SMTP.user).toString('base64'), [334], 1);
          await cmd(Buffer.from(SMTP.pass || '').toString('base64'), [235], 1);
        }
      }
      const B = 'b_' + rnd(12), from = clean(SMTP.from);
      const msg = [
        `From: ${/</.test(from) ? from : `"${SITE}" <${from}>`}`, `To: <${to}>`, `Subject: ${encWord(clean(subject))}`,
        `Date: ${new Date().toUTCString()}`, `Message-ID: <${rnd(12)}@${addr(from).split('@')[1] || me}>`,
        'MIME-Version: 1.0', `Content-Type: multipart/alternative; boundary="${B}"`, '',
        `--${B}`, 'Content-Type: text/plain; charset=utf-8', 'Content-Transfer-Encoding: base64', '', b64(text),
        `--${B}`, 'Content-Type: text/html; charset=utf-8', 'Content-Transfer-Encoding: base64', '', b64(html),
        `--${B}--`, ''].join('\r\n');
      await cmd(`MAIL FROM:<${addr(from)}>`, [250]);
      await cmd(`RCPT TO:<${to}>`, [250, 251]);
      await cmd('DATA', [354]);
      await cmd(msg + '\r\n.', [250]);          // base64 bodies never start a line with "."
      sock.write('QUIT\r\n'); done = true; clearTimeout(timer); sock.end(); resolve();
    })().catch(e => { clearTimeout(timer); fail(e); });
  });
}
const h = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
function mailBody(title, lines, link, button, foot) {
  const text = [title, '', ...lines, '', link, '', foot].join('\n');
  const html = `<div style="font-family:system-ui,Segoe UI,Roboto,sans-serif;max-width:480px;margin:auto;padding:24px;background:#0a1a48;color:#f5f8ff;border-radius:14px">
<h2 style="color:#ffcd46;margin:0 0 12px">🏆 ${h(title)}</h2>${lines.map(l => `<p>${h(l)}</p>`).join('')}
<p style="text-align:center;margin:24px 0"><a href="${h(link)}" style="background:#ffcd46;color:#080e30;padding:12px 22px;border-radius:10px;font-weight:800;text-decoration:none">${h(button)}</a></p>
<p style="font-size:12px;color:#96afeb;word-break:break-all">${h(link)}</p><p style="font-size:12px;color:#96afeb">${h(foot)}</p></div>`;
  return { text, html };
}

// ---------- request helpers ----------
const SEC = { 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
  'Cross-Origin-Opener-Policy': 'same-origin', 'Cross-Origin-Resource-Policy': 'same-origin',
  'Permissions-Policy': 'camera=(), microphone=(), geolocation=(), payment=()' };
const send = (res, code, obj, hd = {}) => { res.writeHead(code, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...SEC, ...hd }); res.end(JSON.stringify(obj)); };
const cookies = req => Object.fromEntries((req.headers.cookie || '').split(';').map(s => { const i = s.indexOf('='); return [s.slice(0, i).trim(), s.slice(i + 1).trim()]; }).filter(a => a[0]));
const ipOf = req => (TRUST_PROXY && String(req.headers['x-forwarded-for'] || '').split(',')[0].trim()) || req.socket.remoteAddress;
const isHttps = req => E.COOKIE_SECURE === '1' || (TRUST_PROXY && req.headers['x-forwarded-proto'] === 'https') || APP_URL.startsWith('https:');
const CK = req => isHttps(req) ? '__Host-s' : 's';
const readBody = (req, max) => new Promise((ok, no) => {
  let d = '', n = 0;
  req.on('data', c => { n += c.length; if (n > max) { no(Object.assign(new Error('too big'), { code: 413 })); req.destroy(); } else d += c; });
  req.on('end', () => ok(d)); req.on('error', no);
});
async function json(req, max = 10000) {
  if (!(req.headers['content-type'] || '').includes('application/json')) throw Object.assign(new Error('json only'), { code: 415 });
  try { const v = JSON.parse(await readBody(req, max) || '{}'); if (v && typeof v === 'object') return v; } catch (e) { if (e.code) throw e; }
  throw Object.assign(new Error('bad json'), { code: 400 });
}
function session(req) {
  const t = cookies(req)[CK(req)]; if (!t || !/^[0-9a-f]{64}$/.test(t)) return null;
  const s = db.prepare('SELECT s.csrf, s.expires, a.* FROM sessions s JOIN admins a ON a.id=s.admin_id WHERE s.hash=?').get(sha(t));
  return s && s.expires > Date.now() && s.hash ? s : null;
}
function startSession(req, res, a, ip) {
  const token = rnd(), csrf = rnd(16);
  db.prepare('INSERT INTO sessions(hash,admin_id,csrf,expires,ip) VALUES(?,?,?,?,?)').run(sha(token), a.id, csrf, Date.now() + SESSION_MS, ip);
  db.prepare('UPDATE admins SET last_login=? WHERE id=?').run(Date.now(), a.id);
  const sec = isHttps(req) ? '; Secure' : '';
  return `${CK(req)}=${token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=${SESSION_MS / 1000}${sec}`;
}
const clearCookie = req => `${CK(req)}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0${isHttps(req) ? '; Secure' : ''}`;
function baseUrl(req) {
  if (APP_URL) return APP_URL;
  const proto = TRUST_PROXY && req.headers['x-forwarded-proto'] === 'https' ? 'https' : 'http';
  return `${proto}://${req.headers.host}`;
}
// Same-origin check for every state-changing request (on top of SameSite=Strict + CSRF token).
function sameOrigin(req) {
  const o = req.headers.origin || req.headers.referer; if (!o) return true;   // non-browser clients
  try { const u = new URL(o); return u.host === req.headers.host || (APP_URL && u.origin === new URL(APP_URL).origin); } catch { return false; }
}

// Validate the league state so a stored payload can't carry markup or junk.
function validState(s) {
  if (!s || typeof s !== 'object' || Array.isArray(s) || !s.g || typeof s.g !== 'object') return false;
  const keys = Object.keys(s.g); if (keys.length < 1 || keys.length > 8) return false;
  for (const k of keys) {
    if (!/^[A-H]$/.test(k) || !Array.isArray(s.g[k]) || s.g[k].length > 6) return false;
    if (!s.g[k].every(n => typeof n === 'string' && n.length >= 1 && n.length <= 40)) return false;
  }
  for (const f of ['r', 'k', 'st', 'cfg', 'aw', 'ui', 'ev', 'dl']) if (s[f] != null && (typeof s[f] !== 'object' || Array.isArray(s[f]))) return false;
  if (s.kd != null && (!Array.isArray(s.kd) || s.kd.length > 24 || !s.kd.every(k => typeof k === 'string' && /^[A-H][123]$/.test(k)))) return false;
  if (s.cfg && s.cfg.kdr != null && s.cfg.kdr !== 0 && s.cfg.kdr !== 1) return false;
  if (s.ui) for (const v of Object.values(s.ui)) if (typeof v !== 'string' || v.length > 40) return false;
  if (s.ev) for (const [k, list] of Object.entries(s.ev)) {
    if (!/^(r\.[A-H]\d+_\d+|k\.(r\d+t\d+\.l[12]|f\.s))$/.test(k) || !Array.isArray(list) || list.length > 30) return false;
    for (const e of list) if (!e || (e.s !== 0 && e.s !== 1) || (e.m != null && !(Number.isInteger(e.m) && e.m >= 0 && e.m <= 130)) ||
      typeof (e.p ?? '') !== 'string' || typeof (e.a ?? '') !== 'string' || (e.p || '').length > 30 || (e.a || '').length > 30) return false;
  }
  return true;
}

const DUMMY_SALT = rnd(16);
const readState = () => { const r = db.prepare('SELECT json FROM state WHERE id=1').get(); return r ? JSON.parse(r.json) : {}; };
// Every write bumps `rev`, so an editor working on an old copy can't silently overwrite newer changes.
function writeState(s) {
  s.rev = (readState().rev || 0) + 1;
  db.prepare(`INSERT INTO state(id,json,updated) VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET json=excluded.json, updated=excluded.updated`)
    .run(JSON.stringify(s), new Date().toISOString());
  return s.rev;
}
// Fisher–Yates with the OS's cryptographic random generator: nobody, including admins, can predict or steer it.
function shuffle(a) { a = a.slice(); for (let i = a.length - 1; i > 0; i--) { const j = crypto.randomInt(i + 1); [a[i], a[j]] = [a[j], a[i]]; } return a; }
function recordDraw(me, kind, result) {
  const ts = Date.now();
  const id = db.prepare('INSERT INTO draws(ts,by,role,kind,result) VALUES(?,?,?,?,?)').run(ts, me.username, me.role, kind, JSON.stringify(result)).lastInsertRowid;
  return { id: Number(id), ts, kind };
}
const ok = { ok: true };
const generic = 'Wrong username or password.';

// ---------- starting league ----------
// The players, groups and settings a brand-new league starts with live in seed.json (not in the page's code).
// They're copied into the database on first start; after that the database is the only source of truth.
const SEED_FILE = E.SEED_FILE || path.join(DIR, 'seed.json');
function seed() {
  let s = {};
  try { s = JSON.parse(fs.readFileSync(SEED_FILE, 'utf8')); } catch (e) { if (e.code !== 'ENOENT') console.error(`Could not read ${SEED_FILE}:`, e.message); }
  s = { r: {}, k: {}, st: {}, ev: {}, aw: { bd: '', c: [] }, ...s };
  if (!validState(s)) {
    if (s.g) console.error(`${SEED_FILE} is not a valid league (1–8 groups A–H, up to 6 names of 1–40 characters each). Using a blank league.`);
    s = { r: {}, k: {}, st: {}, ev: {}, aw: { bd: '', c: [] }, g: { A: ['Player 1', 'Player 2', 'Player 3', 'Player 4'], B: ['Player 5', 'Player 6', 'Player 7', 'Player 8'] } };
  }
  return s;
}
if (!db.prepare('SELECT 1 AS x FROM state WHERE id=1').get()) { writeState(seed()); console.log(`New league created from ${path.basename(SEED_FILE)}.`); }

// ---------- routes ----------
const routes = {
  'GET /api/seed': (req, res, { me }) => me ? send(res, 200, seed()) : send(res, 401, { error: 'login required' }),
  'GET /api/state': (req, res) => send(res, 200, readState()),
  'GET /api/health': (req, res) => { db.prepare('SELECT 1 AS x').get(); send(res, 200, { ok: true }); },
  'GET /api/me': (req, res, { me }) => send(res, 200, me
    ? { admin: true, user: pub(me), csrf: me.csrf, smtp: smtpReady() }
    : { admin: false, resetEnabled: smtpReady() && !!APP_URL }),

  'POST /api/login': async (req, res, { ip }) => {
    if (over('ip:' + ip, 10, 900e3)) return send(res, 429, { error: 'Too many failed attempts. Try again in 15 minutes.' });
    const b = await json(req);
    const id = typeof b.user === 'string' ? b.user.trim().slice(0, 254) : '', pw = typeof b.password === 'string' ? b.password.slice(0, 200) : '';
    const a = id && db.prepare('SELECT * FROM admins WHERE username=? OR email=?').get(id, id);
    const acct = 'acct:' + (a ? a.id : id.toLowerCase());
    if (over(acct, 5, 900e3)) return send(res, 429, { error: 'This account is locked for 15 minutes after too many failed attempts.' });
    const x = Buffer.from(await scrypt(pw, a && a.salt || DUMMY_SALT), 'hex');   // always hash: no timing leak for unknown users
    const y = a && a.hash ? Buffer.from(a.hash, 'hex') : crypto.randomBytes(64);
    if (!(a && a.hash && x.length === y.length && crypto.timingSafeEqual(x, y))) {
      hit('ip:' + ip, 10, 900e3); hit(acct, 5, 900e3);
      audit(id || '?', 'failed login', ip);
      return send(res, 401, { error: generic });
    }
    buckets.delete(acct);
    audit(a.username, 'logged in', ip);
    send(res, 200, { ok: true, user: pub(a) }, { 'Set-Cookie': startSession(req, res, a, ip) });
  },
  'POST /api/logout': (req, res, { me }) => {
    const t = cookies(req)[CK(req)]; if (t) db.prepare('DELETE FROM sessions WHERE hash=?').run(sha(t));
    send(res, 200, ok, { 'Set-Cookie': clearCookie(req) });
  },
  'POST /api/logout-all': (req, res, { me, ip }) => {
    if (!me) return send(res, 401, { error: 'login required' });
    db.prepare('DELETE FROM sessions WHERE admin_id=?').run(me.id); audit(me.username, 'logged out everywhere', ip);
    send(res, 200, ok, { 'Set-Cookie': clearCookie(req) });
  },

  'PUT /api/state': async (req, res, { me, ip }) => {
    if (!me) return send(res, 401, { error: 'login required' });
    if (me.must_change) return send(res, 403, { error: 'Choose a new password first.' });
    const s = await json(req, 1e6); delete s.rev;
    if (!validState(s)) return send(res, 400, { error: 'bad state' });
    const prev = readState();
    if (String(req.headers['x-rev']) !== String(prev.rev || 0))
      return send(res, 409, { conflict: true, error: 'Another editor changed the league at the same time. The latest version has been loaded, so please redo your last change.' });
    if (s.kd != null && JSON.stringify(s.kd) !== JSON.stringify(prev.kd ?? null))
      return send(res, 409, { error: 'The knockout draw can only be made with the "Draw the knockouts" button.' });
    if (prev.dl) s.dl = prev.dl; else delete s.dl;      // the latest-draw marker is set by the server only
    const rev = writeState(s);
    if (!hit('save:' + me.id, 1, 300e3)) audit(me.username, 'edited the league', ip);   // at most one log line per 5 min
    send(res, 200, { ok: true, rev });
  },

  'POST /api/password': async (req, res, { me, ip }) => {
    if (!me) return send(res, 401, { error: 'login required' });
    if (hit('pwchg:' + me.id, 10, 900e3)) return send(res, 429, { error: 'Too many attempts. Try again later.' });
    const b = await json(req);
    const cur = Buffer.from(await scrypt(String(b.current || '').slice(0, 200), me.salt), 'hex');
    if (!crypto.timingSafeEqual(cur, Buffer.from(me.hash, 'hex'))) return send(res, 400, { error: 'Current password is wrong.' });
    const p = pwProblem(b.password, me.username); if (p) return send(res, 400, { error: p });
    if (b.password === b.current) return send(res, 400, { error: 'Pick a password different from the current one.' });
    await setPassword(me.id, b.password); audit(me.username, 'changed password', ip);
    send(res, 200, ok, { 'Set-Cookie': startSession(req, res, me, ip) });
  },
  'POST /api/forgot': async (req, res, { ip }) => {
    const b = await json(req), id = String(b.user || '').trim().slice(0, 254);
    const answer = { ok: true, message: 'If that account has an email address, a reset link is on its way.' };
    if (!smtpReady() || !APP_URL) return send(res, 400, { error: 'Password reset by email is not set up. Ask the owner to help.' });
    if (hit('forgot:' + ip, 5, 3600e3)) return send(res, 200, answer);
    const a = id && db.prepare('SELECT * FROM admins WHERE username=? OR email=?').get(id, id);
    if (a && a.email && a.hash && !hit('forgot:' + a.id, 3, 3600e3)) {
      const t = rnd(); db.prepare("DELETE FROM tokens WHERE admin_id=? AND kind='reset'").run(a.id);
      db.prepare("INSERT INTO tokens(hash,admin_id,kind,expires) VALUES(?,?,'reset',?)").run(sha(t), a.id, Date.now() + RESET_MS);
      const link = `${APP_URL}/#reset=${t}`;
      sendMail({ to: a.email, subject: `Reset your ${SITE} password`, ...mailBody('Reset your password',
        [`Hi ${a.username},`, 'Someone (hopefully you) asked to reset your admin password. The link works once and expires in 1 hour.'],
        link, 'Choose a new password', "If you didn't ask for this you can ignore this email; your password stays the same.") })
        .then(() => audit(a.username, 'reset email sent', ip)).catch(e => { console.error('Reset email failed:', e.message); audit(a.username, 'reset email FAILED', ip); });
    }
    send(res, 200, answer);
  },
  'POST /api/token/check': async (req, res, { ip }) => {
    if (hit('tok:' + ip, 30, 900e3)) return send(res, 429, { error: 'Too many attempts. Try again later.' });
    const b = await json(req), t = String(b.token || '');
    const r = /^[0-9a-f]{64}$/.test(t) && db.prepare('SELECT t.kind, t.expires, a.username FROM tokens t JOIN admins a ON a.id=t.admin_id WHERE t.hash=?').get(sha(t));
    if (!r || r.expires < Date.now()) return send(res, 400, { error: 'This link is invalid or has expired. Ask for a new one.' });
    send(res, 200, { kind: r.kind, username: r.username });
  },
  'POST /api/token/use': async (req, res, { ip }) => {
    if (hit('tok:' + ip, 30, 900e3)) return send(res, 429, { error: 'Too many attempts. Try again later.' });
    const b = await json(req), t = String(b.token || '');
    const r = /^[0-9a-f]{64}$/.test(t) && db.prepare('SELECT t.kind, t.expires, a.* FROM tokens t JOIN admins a ON a.id=t.admin_id WHERE t.hash=?').get(sha(t));
    if (!r || r.expires < Date.now()) return send(res, 400, { error: 'This link is invalid or has expired. Ask for a new one.' });
    const p = pwProblem(b.password, r.username); if (p) return send(res, 400, { error: p });
    await setPassword(r.id, b.password);           // also burns every token for this admin
    audit(r.username, r.kind === 'invite' ? 'accepted invite' : 'reset password by email', ip);
    send(res, 200, { ok: true }, { 'Set-Cookie': startSession(req, res, r, ip) });
  },

  // ----- official random draws -----
  // Only the current draws: the latest group draw, plus the knockout draw made after it (if any).
  'GET /api/draws': (req, res) => {
    const g = db.prepare("SELECT * FROM draws WHERE kind='groups' ORDER BY id DESC LIMIT 1").get();
    const k = db.prepare("SELECT * FROM draws WHERE kind='knockout' AND id>? ORDER BY id DESC LIMIT 1").get(g ? g.id : 0);
    const roleOf = d => d.role || (db.prepare('SELECT role FROM admins WHERE username=?').get(d.by) || {}).role || 'admin';
    send(res, 200, { draws: [k, g].filter(Boolean).map(d => ({ id: d.id, ts: d.ts, by: d.by, role: roleOf(d), kind: d.kind, result: JSON.parse(d.result) })) });
  },
  'POST /api/draw/groups': async (req, res, { me, ip }) => {
    if (!me) return send(res, 401, { error: 'login required' });
    const b = await json(req, 20000), G = +b.groups, seen = new Set();
    const names = (Array.isArray(b.players) ? b.players : []).map(n => String(n).trim()).filter(Boolean);
    if (!(G >= 2 && G <= 8)) return send(res, 400, { error: 'Choose 2 to 8 groups.' });
    for (const n of names) {
      if (n.length > 40) return send(res, 400, { error: `Name too long: ${n.slice(0, 20)}…` });
      if (seen.has(n.toLowerCase())) return send(res, 400, { error: `"${n}" is on the list twice.` });
      seen.add(n.toLowerCase());
    }
    if (names.length < G * 2) return send(res, 400, { error: `You need at least ${G * 2} players for ${G} groups (2 per group).` });
    if (names.length > G * 6) return send(res, 400, { error: `Too many players: at most ${G * 6} for ${G} groups (6 per group).` });
    // With pots the list is strongest-first: each pot of G players is spread one per group, so top players can't meet early.
    const L = 'ABCDEFGH'.slice(0, G).split(''), g = Object.fromEntries(L.map(x => [x, []])), order = [];
    const pots = b.pots ? Array.from({ length: Math.ceil(names.length / G) }, (_, i) => names.slice(i * G, i * G + G)) : [names];
    for (const pot of pots) {
      const drawn = shuffle(pot);
      // Fill the emptiest groups first, in a random order, so group sizes never differ by more than one.
      let slots = [];
      while (slots.length < drawn.length) {
        const min = Math.min(...L.map(x => g[x].length + slots.filter(y => y === x).length));
        slots = slots.concat(shuffle(L.filter(x => g[x].length + slots.filter(y => y === x).length === min)));
      }
      drawn.forEach((n, i) => { g[slots[i]].push(n); order.push([slots[i], n]); });
    }
    const prev = readState();
    const dl = recordDraw(me, 'groups', { groups: g, order, pots: !!b.pots });
    const s = { g, r: {}, k: {}, st: {}, ev: {}, cfg: prev.cfg || {}, ui: prev.ui, aw: { bd: '', c: ((prev.aw && prev.aw.c) || []).map(c => ({ t: c.t, n: '' })) }, dl };
    writeState(s); audit(me.username, `ran the group draw (#${dl.id})`, ip);
    send(res, 200, { state: s, draw: dl, result: { groups: g, order, pots: !!b.pots } });
  },
  'POST /api/draw/knockout': async (req, res, { me, ip }) => {
    if (!me) return send(res, 401, { error: 'login required' });
    const b = await json(req), prev = readState();
    if (prev.kd && prev.kd.length) return send(res, 409, { error: 'The knockouts have already been drawn.' });
    const pots = Array.isArray(b.pots) ? b.pots : [];
    if (pots.length !== 3 || !pots.every(p => Array.isArray(p) && p.length <= 8 &&
        p.every(x => x && /^[A-H][123]$/.test(x.k) && typeof x.n === 'string' && x.n.length <= 40 && prev.g && prev.g[x.k[0]])))
      return send(res, 400, { error: 'bad draw request' });
    const keys = pots.flat().map(x => x.k);
    if (keys.length < 2 || new Set(keys).size !== keys.length) return send(res, 400, { error: 'bad draw request' });
    const drawn = pots.map(shuffle);
    prev.kd = drawn.flat().map(x => x.k); prev.k = {};
    for (const k of Object.keys(prev.ev || {})) if (k.startsWith('k.')) delete prev.ev[k];
    prev.dl = recordDraw(me, 'knockout', { pots: drawn });
    writeState(prev); audit(me.username, `ran the knockout draw (#${prev.dl.id})`, ip);
    send(res, 200, { state: prev, draw: prev.dl, result: { pots: drawn } });
  },

  // ----- admin management -----
  'GET /api/admins': (req, res, { me }) => {
    if (!me) return send(res, 401, { error: 'login required' });
    const admins = db.prepare('SELECT * FROM admins ORDER BY role DESC, created').all().map(pub);
    const log = me.role === 'owner' ? db.prepare('SELECT ts, actor, action, ip FROM audit ORDER BY id DESC LIMIT 40').all() : [];
    send(res, 200, { admins, log, smtp: smtpReady() });
  },
  'POST /api/admins': async (req, res, { me, ip }) => {
    if (!me) return send(res, 401, { error: 'login required' });
    if (hit('invite:' + me.id, 20, 3600e3)) return send(res, 429, { error: 'Too many invites. Try again later.' });
    const b = await json(req), username = String(b.username || '').trim(), email = String(b.email || '').trim();
    if (!USER_RE.test(username)) return send(res, 400, { error: 'Username: 3–32 letters, numbers, dot, dash or underscore.' });
    if (!EMAIL_RE.test(email)) return send(res, 400, { error: 'Enter a valid email address.' });
    if (db.prepare('SELECT 1 AS x FROM admins WHERE username=? OR email=? OR username=? OR email=?').get(username, email, email, username))
      return send(res, 409, { error: 'An admin with that username or email already exists.' });
    db.prepare("INSERT INTO admins(username,email,role,created,invited_by) VALUES(?,?,'admin',?,?)").run(username, email, Date.now(), me.username);
    const a = db.prepare('SELECT * FROM admins WHERE username=?').get(username);
    audit(me.username, `invited ${username} <${email}>`, ip);
    send(res, 200, await invite(req, me, a, ip));
  },
  'POST /api/admins/resend': async (req, res, { me, ip }) => {
    if (!me) return send(res, 401, { error: 'login required' });
    if (hit('invite:' + me.id, 20, 3600e3)) return send(res, 429, { error: 'Too many invites. Try again later.' });
    const b = await json(req), a = db.prepare('SELECT * FROM admins WHERE id=?').get(+b.id);
    if (!a || a.hash) return send(res, 400, { error: 'That admin has already joined.' });
    audit(me.username, `re-sent invite to ${a.username}`, ip);
    send(res, 200, await invite(req, me, a, ip));
  },
  'POST /api/admins/remove': async (req, res, { me, ip }) => {
    if (!me) return send(res, 401, { error: 'login required' });
    const b = await json(req), a = db.prepare('SELECT * FROM admins WHERE id=?').get(+b.id);
    if (!a) return send(res, 404, { error: 'Not found' });
    if (a.role === 'owner') return send(res, 403, { error: "The owner can't be removed." });
    if (me.role !== 'owner' && a.id !== me.id && !(a.hash == null && a.invited_by === me.username))
      return send(res, 403, { error: 'Only the owner can remove other admins.' });
    db.prepare('DELETE FROM admins WHERE id=?').run(a.id);       // sessions + tokens cascade
    audit(me.username, a.id === me.id ? 'left the admin team' : `removed admin ${a.username}`, ip);
    send(res, 200, ok);
  },
  'POST /api/email': async (req, res, { me, ip }) => {          // update own email
    if (!me) return send(res, 401, { error: 'login required' });
    const b = await json(req), email = String(b.email || '').trim();
    if (!EMAIL_RE.test(email)) return send(res, 400, { error: 'Enter a valid email address.' });
    if (db.prepare('SELECT 1 AS x FROM admins WHERE (email=? OR username=?) AND id<>?').get(email, email, me.id)) return send(res, 409, { error: 'That email is already used.' });
    db.prepare('UPDATE admins SET email=? WHERE id=?').run(email, me.id); audit(me.username, 'changed email', ip);
    send(res, 200, ok);
  },
  'POST /api/smtp/test': async (req, res, { me, ip }) => {
    if (!me) return send(res, 401, { error: 'login required' });
    if (!me.email) return send(res, 400, { error: 'Add your own email address first.' });
    if (hit('smtptest:' + me.id, 5, 3600e3)) return send(res, 429, { error: 'Too many test emails. Try again later.' });
    try {
      await sendMail({ to: me.email, subject: `${SITE}: test email`, ...mailBody('SMTP works ✔', [`Hi ${me.username},`, 'Your mail settings are working.'], baseUrl(req), 'Open the league', 'Sent from the admin panel.') });
      send(res, 200, ok);
    } catch (e) { send(res, 502, { error: 'Sending failed: ' + e.message }); }
  },
};
async function invite(req, me, a, ip) {
  db.prepare("DELETE FROM tokens WHERE admin_id=? AND kind='invite'").run(a.id);
  const t = rnd(); db.prepare("INSERT INTO tokens(hash,admin_id,kind,expires) VALUES(?,?,'invite',?)").run(sha(t), a.id, Date.now() + INVITE_MS);
  const link = `${baseUrl(req)}/#invite=${t}`;
  if (!smtpReady()) return { ok: true, emailed: false, link, note: 'SMTP is not set up, so send this link yourself. It expires in 48 hours.' };
  try {
    await sendMail({ to: a.email, subject: `You're invited to run ${SITE}`, ...mailBody("You're invited as an admin",
      [`Hi ${a.username},`, `${me.username} added you as an admin of ${SITE}. Choose your password to get started. The link works once and expires in 48 hours.`, `Your username: ${a.username}`],
      link, 'Set my password', "If you weren't expecting this, ignore the email.") });
    return { ok: true, emailed: true };
  } catch (e) {
    console.error('Invite email failed:', e.message); audit(me.username, `invite email to ${a.username} FAILED`, ip);
    return { ok: true, emailed: false, link, note: `The email could not be sent (${e.message}). Share this link instead. It expires in 48 hours.` };
  }
}

const PAGE = path.join(DIR, 'public', 'index.html');
const server = http.createServer(async (req, res) => {
  try {
    const url = req.url.split('?')[0], ip = ipOf(req), key = `${req.method} ${url}`;
    if (req.method === 'GET' && (url === '/' || url === '/index.html')) {
      const nonce = crypto.randomBytes(16).toString('base64');
      const csp = `default-src 'self'; script-src 'nonce-${nonce}'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; ` +
        `object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`;
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store', 'Content-Security-Policy': csp, ...SEC,
        ...(isHttps(req) ? { 'Strict-Transport-Security': 'max-age=31536000; includeSubDomains' } : {}) });
      return res.end(fs.readFileSync(PAGE, 'utf8').replace(/<script>/g, `<script nonce="${nonce}">`));
    }
    const fn = routes[key];
    if (!fn) return send(res, 404, { error: 'not found' });
    const me = session(req);
    if (req.method !== 'GET') {
      if (!sameOrigin(req)) return send(res, 403, { error: 'bad origin' });
      // Logged-in requests must echo the per-session CSRF token.
      if (me && req.headers['x-csrf'] !== me.csrf && key !== 'POST /api/logout') return send(res, 403, { error: 'Session check failed. Reload the page.' });
    }
    const open = ['GET /api/health', 'GET /api/state', 'GET /api/me', 'POST /api/password', 'POST /api/logout', 'POST /api/logout-all'];
    if (me && me.must_change && !open.includes(key)) return send(res, 403, { error: 'Choose a new password first.' });
    await fn(req, res, { me, ip });
  } catch (e) {
    if (e.code >= 400 && e.code < 500) return send(res, e.code, { error: e.message });
    console.error(e); send(res, 500, { error: 'server error' });
  }
});
server.headersTimeout = 10000; server.requestTimeout = 20000; server.keepAliveTimeout = 5000;
ready.then(() => server.listen(PORT, () => {
  console.log(`League site running on http://localhost:${PORT}`);
  console.log(smtpReady() ? `Email: sending through ${SMTP.host}:${SMTP.port}` : 'Email: SMTP not set (invite links will be shown in the admin panel instead).');
  if (!APP_URL) console.log('Tip: set APP_URL (e.g. https://league.example.com) so emailed links are correct and password reset is enabled.');
}));

process.on('unhandledRejection', e => console.error('Unhandled error:', e));
// Hosts stop containers with SIGTERM: finish open requests, then close the database cleanly.
for (const sig of ['SIGTERM', 'SIGINT']) process.on(sig, () => {
  console.log(`${sig} received, shutting down…`);
  server.close(() => { try { db.close(); } catch {} process.exit(0); });
  setTimeout(() => process.exit(0), 5000).unref();
});
