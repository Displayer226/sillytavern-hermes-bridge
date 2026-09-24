# Secret scan scope and Hermes audit

## Blocking scan for this repository

The `secret-scan` CI job checks this repository's tracked root files and its
Git history. It checks out full root history and does not initialize
submodules. The file scan reconstructs blobs from the root commit and skips
gitlinks, so it never scans the Hermes working tree. The history scan uses the
root repository's Git objects; it does not follow a gitlink into another
repository. Both scans withhold detected values from their logs.

## Separate Hermes audit

Hermes Agent is pinned as a submodule at
`5661709c997cb5557cc337fd428b44c598ab43ca`. A separate content scan of that
pinned tree reported **864 Gitleaks findings**. These findings have not been
suppressed, removed, or declared resolved, and this count is not part of the
blocking root-repository scan.

The previous review classified 855 matches as fixtures or examples. The nine
remaining matches were reviewed locally: five are OAuth client identifiers,
one is a false positive caused by the `max_tokens` parameter name in
`agent/context_compressor.py`, one is an environment-variable label in the
migration mapping, one is a public default voice identifier, and one is the
public search-only key in the Algolia client configuration. This is a manual
classification, not a Gitleaks exclusion; no global allowlist was added.

The 864 findings remain detectable in the pinned Hermes tree. Re-running its
scan is a separate audit task and must keep its output value-redacted. Any new
or unclassified finding in Hermes requires review in that audit rather than
being treated as cleared by the root CI result.
