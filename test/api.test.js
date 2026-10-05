// End-to-end API tests: starts the real server on a throwaway database. Run with `npm test`.
const { test, before, after } = require('node:test');
const assert = require('node:assert');
const { spawn } = require('node:child_process');
const fs = require('node:fs'), os = require('node:os'), path = require('node:path');

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'league-test-'));
const PORT = 4100 + Math.floor(Math.random() * 800), B = `http://127.0.0.1:${PORT}`;
const USER = 'owner', PASS = 'A long test password 1';
let srv;

before(async () => {
  srv = spawn(process.execPath, [path.join(__dirname, '..', 'server.js')], {
    env: { ...process.env, PORT: String(PORT), DB_FILE: path.join(dir, 'league.db'), ADMIN_USER: USER, ADMIN_PASSWORD: PASS,
      ADMIN_EMAIL: '', APP_URL: '', SMTP_HOST: '', TRUST_PROXY: '' },
    stdio: 'ignore',
  });
  for (let i = 0; i < 50; i++) {
    try { if ((await fetch(B + '/api/health')).ok) return; } catch {}
    await new Promise(r => setTimeout(r, 100));
  }
  throw new Error('server did not start');
});
after(async () => {
  const exited = new Promise(r => srv.once('exit', r));
  srv.kill(); await exited;
  try { fs.rmSync(dir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 }); } catch {}   // Windows may hold the file briefly
});

const J = { 'Content-Type': 'application/json' };
async function login(user = USER, password = PASS) {
  const r = await fetch(B + '/api/login', { method: 'POST', headers: J, body: JSON.stringify({ user, password }) });
  if (!r.ok) return { status: r.status, body: await r.json() };
  const cookie = r.headers.get('set-cookie').split(';')[0];
  const me = await (await fetch(B + '/api/me', { headers: { cookie } })).json();
  return { status: 200, cookie, csrf: me.csrf, me };
}
const call = (s, method, url, body, extra = {}) => fetch(B + url, { method, headers: { ...J, cookie: s.cookie, 'X-CSRF': s.csrf, ...extra }, body: body && JSON.stringify(body) });
const rev = async () => (await (await fetch(B + '/api/state')).json()).rev || 0;
const league = { g: { A: ['a1', 'a2', 'a3', 'a4'], B: ['b1', 'b2', 'b3', 'b4'] }, r: {}, k: {} };

test('health check and public pages', async () => {
  assert.equal((await fetch(B + '/api/health')).status, 200);
  const r = await fetch(B + '/');
  assert.equal(r.status, 200);
  assert.match(r.headers.get('content-security-policy'), /script-src 'nonce-/);
  assert.equal(r.headers.get('x-frame-options'), 'DENY');
  assert.match(await r.text(), /<script nonce="/);
});

test('login: wrong password is generic, right one works', async () => {
  const bad = await login(USER, 'nope');
  assert.equal(bad.status, 401);
  const ghost = await login('ghost', 'nope');
  assert.deepEqual(ghost.body, bad.body, 'unknown users get the same answer');
  const s = await login();
  assert.equal(s.status, 200);
  assert.equal(s.me.user.role, 'owner');
});

test('saving needs login, the CSRF token and the current version', async () => {
  assert.equal((await fetch(B + '/api/state', { method: 'PUT', headers: J, body: JSON.stringify(league) })).status, 401);
  const s = await login();
  assert.equal((await call(s, 'PUT', '/api/state', league, { 'X-CSRF': 'wrong', 'X-Rev': String(await rev()) })).status, 403);
  assert.equal((await call(s, 'PUT', '/api/state', league, { 'X-Rev': String(await rev()), Origin: 'http://evil.example' })).status, 403);
  const r0 = await rev();
  const ok = await call(s, 'PUT', '/api/state', league, { 'X-Rev': String(r0) });
  assert.equal(ok.status, 200);
  assert.equal((await ok.json()).rev, r0 + 1);
});

test('two editors: the stale save is refused instead of overwriting', async () => {
  const a = await login(), b = await login();
  const base = await rev();
  assert.equal((await call(a, 'PUT', '/api/state', { ...league, r: { A0_0: [1, 0] } }, { 'X-Rev': String(base) })).status, 200);
  const stale = await call(b, 'PUT', '/api/state', { ...league, r: { A0_0: [5, 5] } }, { 'X-Rev': String(base) });
  assert.equal(stale.status, 409);
  assert.equal((await stale.json()).conflict, true);
  assert.deepEqual((await (await fetch(B + '/api/state')).json()).r.A0_0, [1, 0], "editor A's score survived");
});

test('the server rejects unsafe or broken league data', async () => {
  const s = await login();
  for (const bad of [{ g: { '<img>': ['x', 'y'] } }, { g: { A: ['x'.repeat(41), 'y'] } }, { ...league, ev: { 'r.A0_0': [{ s: 7 }] } }, { ...league, ui: { t: 'x'.repeat(41) } }])
    assert.equal((await call(s, 'PUT', '/api/state', bad, { 'X-Rev': String(await rev()) })).status, 400);
});

test('group draw is random, respects pots, and is published', async () => {
  const s = await login(), seen = new Set();
  const players = ['p1', 'p2', 'p3', 'p4', 'p5', 'p6', 'p7', 'p8'];
  for (let i = 0; i < 12; i++) {
    const r = await call(s, 'POST', '/api/draw/groups', { players, groups: 2, pots: true });
    assert.equal(r.status, 200);
    const { state } = await r.json();
    assert.equal(state.g.A.length + state.g.B.length, 8);
    for (let p = 0; p < 4; p++) {   // each pot of 2 is split across the groups
      const pot = players.slice(p * 2, p * 2 + 2);
      assert.ok(state.g.A.some(n => pot.includes(n)) && state.g.B.some(n => pot.includes(n)));
    }
    seen.add(JSON.stringify(state.g));
  }
  assert.ok(seen.size > 1, 'repeated draws give different groups');
  const dup = await call(s, 'POST', '/api/draw/groups', { players: ['x', 'X', 'y', 'z'], groups: 2 });
  assert.equal(dup.status, 400);
  const pub = await (await fetch(B + '/api/draws')).json();
  assert.ok(pub.draws.length >= 12 && pub.draws[0].kind === 'groups');
});

test('knockout pairings can only come from the server draw, once', async () => {
  const s = await login();
  const forged = await call(s, 'PUT', '/api/state', { ...league, kd: ['A1', 'B1'] }, { 'X-Rev': String(await rev()) });
  assert.equal(forged.status, 409);
  await call(s, 'PUT', '/api/state', league, { 'X-Rev': String(await rev()) });
  const pots = [[{ k: 'A1', n: 'a1' }, { k: 'B1', n: 'b1' }], [{ k: 'A2', n: 'a2' }, { k: 'B2', n: 'b2' }], []];
  const first = await call(s, 'POST', '/api/draw/knockout', { pots });
  assert.equal(first.status, 200);
  assert.equal((await first.json()).state.kd.length, 4);
  assert.equal((await call(s, 'POST', '/api/draw/knockout', { pots })).status, 409, 'no quiet redraws');
});

test('admins: invite link works once, then the new admin can log in', async () => {
  const s = await login();
  const r = await call(s, 'POST', '/api/admins', { username: 'helper', email: 'helper@example.com' });
  const { link } = await r.json();
  const token = link.split('#invite=')[1];
  const use = (password) => fetch(B + '/api/token/use', { method: 'POST', headers: J, body: JSON.stringify({ token, password }) });
  assert.equal((await use('short')).status, 400);
  assert.equal((await use('Another fine password')).status, 200);
  assert.equal((await use('Another fine password')).status, 400, 'link is single-use');
  assert.equal((await login('helper', 'Another fine password')).status, 200);
});

test('repeated wrong passwords lock the account', async () => {
  let last;
  for (let i = 0; i < 6; i++) last = await login('helper', 'wrong password');
  assert.equal(last.status, 429);
});
