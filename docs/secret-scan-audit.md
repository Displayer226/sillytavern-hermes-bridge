# Secret scan scope and Hermes audit

## Blocking scan for this repository

The `secret-scan` CI job checks this repository's tracked root files and its
Git history. It checks out full root history and does not initialize
submodules. The file scan reconstructs blobs from the root commit and skips
gitlinks, so it never scans the Hermes working tree. The history scan uses the
root repository's Git objects; it does not follow a gitlink into another
repository. Both scans withhold detected values from their logs.

## Current Hermes baseline audit

The current public Hermes gitlink is `e8114f7b73be5b0e43ae6edd39055653ad74b09a`. A separate comparison of its complete tree against upstream base `65bc6727b43c05dff410608c78fa055ec194eee0` found **zero new alerts** in the candidate delta. Full scans of both base and candidate reported **926 identical inherited-blob alerts**. These alerts remain detectable; the comparison is not a global clearance, and Hermes is not covered by the blocking root scan. Current Algolia key permissions were not verified and the historical classification below must not be reused as a current clearance.

## Historical Hermes audit at `5661709c`

The initial export pinned Hermes Agent at
`5661709c997cb5557cc337fd428b44c598ab43ca`. A separate content scan of that
historical tree reported **864 Gitleaks findings**. These findings have not been
suppressed, removed, or declared resolved, and this count is not part of the
blocking root-repository scan.

The previous review classified 855 matches as fixtures or examples. The nine
remaining matches were reviewed locally: five are OAuth client identifiers,
one is a false positive caused by the `max_tokens` parameter name in
`agent/context_compressor.py`, one is an environment-variable label in the
migration mapping, one is a public default voice identifier, and one is the
public search-only key in the Algolia client configuration. This is a manual
classification, not a Gitleaks exclusion; no global allowlist was added.

The 864 findings remained detectable in that historical Hermes tree. Re-running its
scan is a separate audit task and must keep its output value-redacted. Any new
or unclassified finding in Hermes requires review in that audit rather than
being treated as cleared by the root CI result.
