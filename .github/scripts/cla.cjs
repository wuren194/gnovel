'use strict';

// Executed only from the trusted base checkout by cla.yml. Never import PR code.
const { createHash } = require('node:crypto');
const STATUS = 'license/cla';
const OWNER_ID = 143167886; // wuren194; only exempt on a PR opened by this account.
const BRANCH = 'cla-signatures';

function validatePR(pr, context) {
  if (pr.number !== context.issue.number || pr.state !== 'open' ||
      pr.base.ref !== 'main' || pr.base.repo.id !== context.payload.repository.id ||
      pr.base.repo.full_name.toLowerCase() !== `${context.repo.owner}/${context.repo.repo}`.toLowerCase() ||
      !/^[a-f0-9]{40}$/.test(pr.head.sha) || !/^[a-f0-9]{40}$/.test(pr.base.sha)) {
    throw new Error('PR is not an open contribution to this repository main branch');
  }
}

async function status(github, context, sha, state, description) {
  await github.rest.repos.createCommitStatus({ ...context.repo, sha, state,
    context: STATUS, description,
    target_url: `${context.serverUrl}/${context.repo.owner}/${context.repo.repo}/actions/runs/${context.runId}` });
}

async function prepare({ github, context, core }) {
  core.setOutput('eligible', 'false');
  const comment = context.payload.comment;
  if (context.eventName === 'issue_comment') {
    if (!context.payload.issue?.pull_request || comment?.user?.type !== 'User' ||
        context.payload.action !== 'created') return;
    const body = comment.body.trim();
    if (body !== 'recheck' && !/^I have read the CLA \([a-f0-9]{64}\) and I hereby sign the CLA$/.test(body)) return;
  } else if (context.eventName !== 'pull_request_target') {
    throw new Error('Unsupported CLA event');
  }
  const { data: pr } = await github.rest.pulls.get({ ...context.repo, pull_number: context.issue.number });
  validatePR(pr, context);
  if (context.eventName === 'pull_request_target' && pr.head.sha !== context.payload.pull_request.head.sha) {
    throw new Error('A newer PR head superseded this event');
  }
  core.setOutput('head', pr.head.sha);
  core.setOutput('base', pr.base.sha);
  await status(github, context, pr.head.sha, 'pending', 'Checking CLA signatures for this contribution');
  const { data: document } = await github.rest.repos.getContent({ ...context.repo, path: 'CLA.md', ref: pr.base.sha });
  if (document.type !== 'file' || document.encoding !== 'base64' || !document.content) {
    throw new Error('CLA.md must be a nonempty ordinary file');
  }
  const digest = createHash('sha256').update(Buffer.from(document.content, 'base64')).digest('hex');
  const phrase = `I have read the CLA (${digest}) and I hereby sign the CLA`;
  if (comment && comment.body.trim() !== 'recheck' && comment.body.trim() !== phrase) {
    throw new Error('The comment signs another CLA version; copy the current bot declaration');
  }
  // The ledger is in this same repository, never on main. No PAT is needed.
  try {
    await github.rest.git.getRef({ ...context.repo, ref: `heads/${BRANCH}` });
  } catch (error) {
    if (error.status !== 404) throw error;
    try {
      await github.rest.git.createRef({ ...context.repo, ref: `refs/heads/${BRANCH}`, sha: pr.base.sha });
    } catch (conflict) {
      if (conflict.status !== 422) throw conflict;
      await github.rest.git.getRef({ ...context.repo, ref: `heads/${BRANCH}` });
    }
  }
  core.setOutput('document', `${context.serverUrl}/${context.repo.owner}/${context.repo.repo}/blob/${pr.base.sha}/CLA.md`);
  core.setOutput('path', `signatures/${digest}/cla.json`);
  core.setOutput('phrase', phrase);
  core.setOutput('allowlist', pr.user.id === OWNER_ID ? String(OWNER_ID) : '');
  core.setOutput('eligible', 'true');
}

async function finish({ github, context, core, head, base, outcome, passed }) {
  if (!head) return; // Preparation failed before a trusted head was established.
  let valid = false;
  try {
    const { data: pr } = await github.rest.pulls.get({ ...context.repo, pull_number: context.issue.number });
    validatePR(pr, context);
    if (pr.head.sha !== head) {
      core.setFailed('PR head changed; wait for the new CLA check');
      return; // Never apply an old result to the new head.
    }
    const { data: ref } = await github.rest.git.getRef({ ...context.repo, ref: 'heads/main' });
    valid = pr.base.sha === base && ref.object.sha === base && outcome === 'success' && passed === 'true';
  } catch (error) {
    core.warning(error.message);
  }
  await status(github, context, head, valid ? 'success' : 'failure', valid
    ? 'Required contributors have signed this CLA version'
    : 'CLA is unsigned, stale, or could not be verified; see workflow');
  if (!valid) core.setFailed('CLA check did not pass; inspect the signer output and recheck');
}

module.exports = { prepare, finish };
