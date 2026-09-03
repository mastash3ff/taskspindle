# Security policy

Report vulnerabilities privately through GitHub security advisories on
`mastash3ff/taskspindle`. Do not open public issues for security reports.

TaskSpindle isolates worker changes in detached Git worktrees and launches workers
with an allowlisted environment. That protects the root workspace from accidental
edits and keeps parent-process secrets out of worker environments. It is not an
operating-system sandbox and does not contain a hostile process.
