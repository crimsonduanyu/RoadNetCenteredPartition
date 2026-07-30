# Security policy

## Supported versions

Security fixes are applied to the current default branch.

## Reporting a vulnerability

Use GitHub's private security-advisory feature for this repository. Do not open
a public issue containing credentials, private data, exploitable paths, or
personal information. If private advisories are unavailable, contact the
repository owner through GitHub without attaching sensitive payloads.

Reports should include the affected version or commit, impact, reproduction
steps that use synthetic data, and a proposed mitigation when available.

## Data incidents

Accidental commits of private orders, driver identifiers, coordinates,
credentials, or local configuration must be treated as incidents. Removing the
file in a later commit is insufficient because Git retains historical blobs.
Revoke exposed credentials immediately and coordinate an authorized
`git filter-repo` history rewrite before publishing or mirroring the repository.
