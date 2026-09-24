// Regression check for the GitHub ingestion node's entity key.
//
//   node n8n/tests/github-ingestion-entity.mjs
//
// The invariant: ONE work item must land in ONE entity group. The node derives it as
// `platform:subject.type:subject.id`, and `subject.type` used to depend on whether the PR's
// detail item happened to be in the same batch — the Pulls API call is a window
// (`state=all`, `sort=updated`, `per_page=50`), so an older PR simply is not in it. A
// comment on such a PR became `github:ticket:owner/repo/N` while the PR's own events were
// `github:pull_request:owner/repo/N`, and entity-scoped behaviour (sibling auto-resolution,
// carry-forward, group summaries, omni grouping) cannot see across the two.
//
// This file executes the node's OWN jsCode out of the workflow JSON rather than a copy, so
// it fails if the node is edited and the invariant is lost. No CI job runs it yet — the
// workflows have no test harness — so it is a check a reviewer can run, not a gate.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const workflow = JSON.parse(fs.readFileSync(path.join(here, '..', 'workflows', 'github-ingestion.json'), 'utf8'));
const node = workflow.nodes.find((n) => n.name === 'Filter & Normalize');
if (!node) throw new Error('node "Filter & Normalize" not found in github-ingestion.json');
const source = node.parameters.jsCode;

const REPO = 'https://api.github.com/repos/org/repo';
const PR_DETAIL = {
  requested_reviewers: [{ login: 'reviewer' }],
  base: { repo: { url: REPO } },
  number: 106, title: 'Fix the thing', user: { login: 'author' }, assignees: [],
};
const PR_COMMENT = {
  id: 9001, issue_url: `${REPO}/issues/106`,
  html_url: 'https://github.com/org/repo/pull/106#issuecomment-9001',
  created_at: '2026-09-24T10:00:00Z', user: { login: 'commenter' }, body: 'looks good',
};
const ISSUE_COMMENT = {
  id: 9002, issue_url: `${REPO}/issues/77`,
  html_url: 'https://github.com/org/repo/issues/77#issuecomment-9002',
  created_at: '2026-09-24T10:00:00Z', user: { login: 'commenter' }, body: 'same bug here',
};

function run(items) {
  const fn = new Function('$input', '$workflow', source);
  return fn({ all: () => items.map((json) => ({ json })) }, { id: 'wf1' })
    .map((o) => o.json)
    .filter(Boolean);
}
const entityOf = (events, id) => events
  .filter((e) => e.subject && e.subject.id === id)
  .map((e) => `${e.source.platform}:${e.subject.type}:${e.subject.id}`);

let failures = 0;
function check(label, actual, expected) {
  const ok = actual === expected;
  if (!ok) failures += 1;
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}\n       got ${actual}\n       want ${expected}`);
}

// 1 + 2: the whole point — with and without the PR detail in the batch, one entity.
const withDetail = entityOf(run([PR_DETAIL, PR_COMMENT]), 'org/repo/106')[0];
const withoutDetail = entityOf(run([PR_COMMENT]), 'org/repo/106')[0];
check('PR comment, detail in the batch', withDetail, 'github:pull_request:org/repo/106');
check('PR comment, detail NOT in the batch (older PR)', withoutDetail, 'github:pull_request:org/repo/106');
check('the two agree', withoutDetail, withDetail);

// 3: an issue comment must stay an issue — the fix must not promote everything to a PR.
check('issue comment stays a ticket', entityOf(run([ISSUE_COMMENT]), 'org/repo/77')[0],
      'github:ticket:org/repo/77');

// 4: when the detail IS present its data is still used (title + reviewer participants).
const enriched = run([PR_DETAIL, PR_COMMENT]).find((e) => e.source.raw_event_type === 'issue_comment_created');
check('title still comes from the PR detail', enriched.subject.title, 'Fix the thing');
check('reviewer still merged into participants',
      enriched.content.metadata.participants.some((p) => p.handle === 'reviewer'), true);

// 5: the html_url test is anchored to `/pull/<number>`, because an owner or repo may
// itself be called `pull`. An unanchored `/\/pull\//` matches the segment in
// `https://github.com/org/pull/issues/77` and promotes a plain issue comment to a PR —
// re-creating the one-work-item-two-entity-groups split this whole file guards.
const issueCommentAt = (id, owner, repo, number, html) => ({
  id,
  issue_url: `https://api.github.com/repos/${owner}/${repo}/issues/${number}`,
  html_url: html,
  created_at: '2026-09-24T10:00:00Z',
  user: { login: 'commenter' },
  body: 'body',
});
check('issue comment under an owner named "pull" stays a ticket',
      entityOf(run([issueCommentAt(9003, 'pull', 'repo', 77,
                                   'https://github.com/pull/repo/issues/77#issuecomment-9003')]),
               'pull/repo/77')[0],
      'github:ticket:pull/repo/77');
check('issue comment under a repo named "pull" stays a ticket',
      entityOf(run([issueCommentAt(9004, 'org', 'pull', 77,
                                   'https://github.com/org/pull/issues/77#issuecomment-9004')]),
               'org/pull/77')[0],
      'github:ticket:org/pull/77');
check('PR comment in a repo named "pull" is still a PR',
      entityOf(run([issueCommentAt(9005, 'org', 'pull', 106,
                                   'https://github.com/org/pull/pull/106#issuecomment-9005')]),
               'org/pull/106')[0],
      'github:pull_request:org/pull/106');

console.log(failures ? `\n${failures} check(s) failed` : '\nall checks passed');
process.exit(failures ? 1 : 0);
