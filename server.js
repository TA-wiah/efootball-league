// eFootball Champions League – zero-dependency Node server with a SQLite .db file.
// Requires Node 22.13+ (uses the built-in node:sqlite module).
const http = require('http'), fs = require('fs'), path = require('path'), crypto = require('crypto');
const { DatabaseSync } = require('node:sqlite');

const PORT = +process.env.PORT || 3000;
const DIR = __dirname;
fs.mkdirSync(path.join(DIR, 'data'), { recursive: true });
const db = new DatabaseSync(process.env.DB_FILE || path.join(DIR, 'data', 'league.db'));
db.exec(`
CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1), json TEXT NOT NULL, updated TEXT);
CREATE TABLE IF NOT EXISTS admin(id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL, salt TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, expires INTEGER NOT NULL);
`);

const hash = (pw, salt) => crypto.scryptSync(pw, salt, 64).toString('hex');
function setAdmin(user, pw) {
  const salt = crypto.randomBytes(16).toString('hex');
  db.prepare(`INSERT INTO admin(id,username,salt,hash) VALUES(1,?,?,?)
    ON CONFLICT(id) DO UPDATE SET username=excluded.username, salt=excluded.salt, hash=excluded.hash`).run(user, salt, hash(pw, salt));
  db.exec('DELETE FROM sessions');
}
if (process.env.ADMIN_PASSWORD) setAdmin(process.env.ADMIN_USER || 'admin', process.env.ADMIN_PASSWORD);
else if (!db.prepare('SELECT 1 AS x FROM admin').get()) {
  setAdmin('admin', 'admin123');
  console.warn('!! Default login is admin / admin123. Set ADMIN_USER and ADMIN_PASSWORD and restart.');
}

const cookies = req => Object.fromEntries((req.headers.cookie || '').split(';').map(s => s.trim().split('=')).filter(a => a[0]));
function isAdmin(req) {
  const t = cookies(req).s; if (!t) return false;
  const r = db.prepare('SELECT expires FROM sessions WHERE token=?').get(t);
  return !!r && r.expires > Date.now();
}
const tries = new Map();
function limited(ip) {
  const now = Date.now(), a = (tries.get(ip) || []).filter(t => now - t < 600000);
  tries.set(ip, a); return a.length >= 8;
}
const send = (res, code, obj, h = {}) => { res.writeHead(code, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...h }); res.end(JSON.stringify(obj)); };
const readBody = (req, max = 1e6) => new Promise((ok, no) => {
  let d = '', n = 0;
  req.on('data', c => { n += c.length; if (n > max) { no(new Error('too big')); req.destroy(); } else d += c; });
  req.on('end', () => ok(d)); req.on('error', no);
});
const isJson = req => (req.headers['content-type'] || '').includes('application/json');

http.createServer(async (req, res) => {
  try {
    const url = req.url.split('?')[0], ip = req.socket.remoteAddress;
    if (req.method === 'GET' && url === '/api/state') {
      const r = db.prepare('SELECT json FROM state WHERE id=1').get();
      return send(res, 200, r ? JSON.parse(r.json) : {});
    }
    if (req.method === 'GET' && url === '/api/me') return send(res, 200, { admin: isAdmin(req) });
    if (req.method === 'POST' && url === '/api/login') {
      if (!isJson(req)) return send(res, 415, { error: 'json only' });
      if (limited(ip)) return send(res, 429, { error: 'Too many attempts, try again in 10 minutes.' });
      const b = JSON.parse(await readBody(req, 5000) || '{}');
      const a = db.prepare('SELECT * FROM admin WHERE id=1').get();
      let ok = false;
      if (a && typeof b.user === 'string' && typeof b.password === 'string') {
        const x = Buffer.from(hash(b.password, a.salt), 'hex'), y = Buffer.from(a.hash, 'hex');
        ok = b.user === a.username && x.length === y.length && crypto.timingSafeEqual(x, y);
      }
      if (!ok) { tries.get(ip).push(Date.now()); return send(res, 401, { error: 'Wrong username or password' }); }
      const token = crypto.randomBytes(32).toString('hex');
      db.prepare('DELETE FROM sessions WHERE expires<?').run(Date.now());
      db.prepare('INSERT INTO sessions(token,expires) VALUES(?,?)').run(token, Date.now() + 7 * 864e5);
      const secure = process.env.COOKIE_SECURE === '1' || req.headers['x-forwarded-proto'] === 'https' ? '; Secure' : '';
      return send(res, 200, { ok: true }, { 'Set-Cookie': `s=${token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=604800${secure}` });
    }
    if (req.method === 'POST' && url === '/api/logout') {
      const t = cookies(req).s; if (t) db.prepare('DELETE FROM sessions WHERE token=?').run(t);
      return send(res, 200, { ok: true }, { 'Set-Cookie': 's=; HttpOnly; Path=/; Max-Age=0' });
    }
    if (req.method === 'PUT' && url === '/api/state') {
      if (!isAdmin(req)) return send(res, 401, { error: 'login required' });
      if (!isJson(req)) return send(res, 415, { error: 'json only' });
      const s = JSON.parse(await readBody(req));
      if (!s || typeof s !== 'object' || typeof s.g !== 'object') return send(res, 400, { error: 'bad state' });
      db.prepare(`INSERT INTO state(id,json,updated) VALUES(1,?,?)
        ON CONFLICT(id) DO UPDATE SET json=excluded.json, updated=excluded.updated`).run(JSON.stringify(s), new Date().toISOString());
      return send(res, 200, { ok: true });
    }
    if (req.method === 'GET' && (url === '/' || url === '/index.html')) {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'same-origin' });
      return res.end(fs.readFileSync(path.join(DIR, 'public', 'index.html')));
    }
    send(res, 404, { error: 'not found' });
  } catch (e) { send(res, 500, { error: 'server error' }); }
}).listen(PORT, () => console.log(`League site running on http://localhost:${PORT}`));
