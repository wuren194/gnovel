'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { prepare, finish } = require('./cla.cjs');

function fixture() {
  const head = 'a'.repeat(40), base = 'b'.repeat(40);
  const pr = { number: 1, state: 'open', head: { sha: head },
    base: { sha: base, ref: 'main', repo: { id: 7, full_name: 'example/project' } }, user: { id: 8 } };
  const context = { repo: { owner: 'example', repo: 'project' }, issue: { number: 1 },
    serverUrl: 'https://github.com', runId: 2, eventName: 'pull_request_target',
    payload: { repository: { id: 7 }, pull_request: structuredClone(pr) } };
  const outputs = {}, statuses = [], failures = [];
  const core = { setOutput: (k, v) => { outputs[k] = v; }, setFailed: m => failures.push(m), warning: () => {} };
  const github = { rest: {
    pulls: { get: async () => ({ data: pr }) },
    repos: { createCommitStatus: async v => statuses.push(v), getContent: async () => ({ data:
      { type: 'file', encoding: 'base64', content: Buffer.from('Example agreement').toString('base64') } }) },
    git: { getRef: async () => ({ data: { object: { sha: base } } }), createRef: async () => {} }
  } };
  return { github, core, context, pr, head, base, outputs, statuses, failures };
}

function comment(f, body) {
  f.context.eventName = 'issue_comment';
  f.context.payload.action = 'created';
  f.context.payload.issue = { pull_request: {}, state: 'open' };
  f.context.payload.comment = { body, user: { type: 'User', id: 8 } };
}

test('prepare binds a signature path and declaration to the same document hash', async () => {
  const f = fixture(); await prepare(f);
  assert.equal(f.outputs.eligible, 'true');
  assert.equal(f.outputs.head, f.head);
  const digest = f.outputs.path.split('/')[1];
  assert.match(digest, /^[a-f0-9]{64}$/);
  assert.ok(f.outputs.phrase.includes(digest));
  assert.ok(f.outputs.document.includes(f.base));
  assert.equal(f.outputs.allowlist, '');
  assert.equal(f.statuses[0].state, 'pending');
});

test('owner exception applies only to a PR opened by that account', async () => {
  const f = fixture(); f.pr.user.id = 143167886;
  await prepare(f); assert.equal(f.outputs.allowlist, '143167886');
});

test('new agreement bytes change both declaration and ledger location', async () => {
  const f = fixture(); await prepare(f); const oldPath = f.outputs.path;
  f.github.rest.repos.getContent = async () => ({ data: { type: 'file', encoding: 'base64', content: Buffer.from('Revised agreement').toString('base64') } });
  await prepare(f); assert.notEqual(f.outputs.path, oldPath);
});

for (const body of ['I will not sign the CLA', 'RECHECK', '> I have read the CLA', 'I have read the CLA (' + 'a'.repeat(64) + ') and I hereby sign the CLA but disagree']) {
  test('unrelated or non-exact comment cannot run signer: ' + body.slice(0, 25), async () => {
    const f = fixture(); comment(f, body); await prepare(f);
    assert.equal(f.outputs.eligible, 'false'); assert.equal(f.statuses.length, 0);
  });
}

test('declaration for an older document is rejected', async () => {
  const f = fixture(); comment(f, `I have read the CLA (${'a'.repeat(64)}) and I hereby sign the CLA`);
  await assert.rejects(prepare(f), /another CLA version/);
  assert.equal(f.outputs.eligible, 'false');
});

test('exact declaration with surrounding line ending is admitted', async () => {
  const f = fixture(); await prepare(f); comment(f, f.outputs.phrase + '\r\n');
  await prepare(f); assert.equal(f.outputs.eligible, 'true');
});

test('stale event cannot initialize a check on a newer head', async () => {
  const f = fixture(); f.pr.head.sha = 'c'.repeat(40);
  await assert.rejects(prepare(f), /newer PR head/); assert.equal(f.statuses.length, 0);
});

test('retargeted PR cannot write a ledger', async () => {
  const f = fixture(); f.pr.base.ref = 'release';
  await assert.rejects(prepare(f), /open contribution/); assert.equal(f.statuses.length, 0);
});

test('wrong base repository is rejected', async () => {
  const f = fixture(); f.pr.base.repo.id = 9;
  await assert.rejects(prepare(f), /open contribution/);
});

test('ledger branch creation handles a concurrent creator by rereading', async () => {
  const f = fixture(); let reads = 0;
  f.github.rest.git.getRef = async () => {
    if (++reads === 1) throw Object.assign(new Error('missing'), { status: 404 });
    return { data: { object: { sha: f.base } } };
  };
  f.github.rest.git.createRef = async () => { throw Object.assign(new Error('exists'), { status: 422 }); };
  await prepare(f); assert.equal(f.outputs.eligible, 'true'); assert.equal(reads, 2);
});

for (const [outcome, passed, expected] of [['success', 'true', 'success'], ['failure', 'true', 'failure'], ['success', 'false', 'failure'], ['skipped', '', 'failure']]) {
  test(`final status requires both signer success and explicit pass (${outcome}/${passed})`, async () => {
    const f = fixture(); await finish({ ...f, outcome, passed });
    assert.equal(f.statuses[0].state, expected);
  });
}

test('a push while signing never receives the old successful status', async () => {
  const f = fixture(); f.pr.head.sha = 'c'.repeat(40);
  await finish({ ...f, outcome: 'success', passed: 'true' });
  assert.equal(f.statuses.length, 0); assert.equal(f.failures.length, 1);
});

test('advancing main invalidates a result even with an unchanged PR head', async () => {
  const f = fixture(); f.github.rest.git.getRef = async () => ({ data: { object: { sha: 'c'.repeat(40) } } });
  await finish({ ...f, outcome: 'success', passed: 'true' }); assert.equal(f.statuses[0].state, 'failure');
});

test('API failure cannot publish success', async () => {
  const f = fixture(); f.github.rest.pulls.get = async () => { throw new Error('API unavailable'); };
  await finish({ ...f, outcome: 'success', passed: 'true' }); assert.equal(f.statuses[0].state, 'failure');
});
