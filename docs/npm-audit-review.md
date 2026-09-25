# npm audit review

Reviewed on 2026-09-24 against `responses-proxy/package-lock.json`. The prior seven development-only alerts were resolved by updating the lockfile in an isolated copy with `npm audit fix --package-lock-only` (without `--force`). After validating that copy, only the verified lockfile was applied to the export; `package.json` and all direct dependency declarations remain unchanged.

`npm audit` on the refreshed lockfile reports **0 vulnerabilities**: 0 critical, 0 high, 0 moderate, and 0 low. No vulnerable direct production dependency was present in the previous lockfile; all seven alerts were transitive development dependencies.

| Package | Previous lock | Updated lock | Audit result |
| --- | ---: | ---: | --- |
| `@babel/core` | 7.24.0 | 7.29.7 | Clear |
| `@babel/plugin-transform-modules-systemjs` | 7.23.9 | 7.29.8 | Clear |
| `baseline-browser-mapping` | 2.9.19 | 2.11.25 | Clear |
| `brace-expansion` | 1.1.13 | 1.1.21 | Clear |
| `browserslist` | 4.28.1 | 4.29.1 | Clear |
| `fast-uri` | 3.0.6 | 3.1.8 | Clear |
| `js-yaml` | 4.1.1 | 4.3.2 | Clear |

The full npm resolution changed 35 lockfile package entries: 30 version updates, two transitive additions, and three removals in the Babel, source-map, and Browserslist dependency graphs. The only semver major transition is `jsesc` 2.5.2 → 3.1.0; `update-browserslist-db` 1.2.3 → 1.3.3 is a minor update. All changed packages declaring a Node engine range accept Node 22; `npm ci` also completed under Node 22.22.1. The lockfile diff contained no package paths beyond this resolution, and no direct dependency was added.

Validation in the isolated copy: `npm ci` installed 574 packages and reported zero vulnerabilities; `npm audit` reported zero; extension unit tests passed; the production build passed with the existing 531 KiB voice bundle size warning; the Playwright voice-browser test passed at 1440×900, 390×844, and 320×568. npm also emitted deprecation notices for existing Babel proposal plugins during installation.

The reviewed advisories covered source-map file reads during compilation ([Babel core](https://github.com/advisories/GHSA-4x5r-pxfx-6jf8)); generated code from crafted compiler input ([Babel SystemJS transform](https://github.com/advisories/GHSA-fv7c-fp4j-7gwp)); process termination on invalid mapping input ([baseline-browser-mapping](https://github.com/advisories/GHSA-w5vr-8v7q-w6rv)); excessive CPU or memory use ([brace-expansion](https://github.com/advisories/GHSA-rgw5-rvv9-x895), [Browserslist](https://github.com/advisories/GHSA-c83g-rgw3-j3cx), [js-yaml](https://github.com/advisories/GHSA-2883-xcg3-v3hh)); and URI host-confusion / normalization issues ([fast-uri](https://github.com/advisories/GHSA-jqff-g426-hqxp)). Re-run `npm audit` regularly and before a versioned release because the advisory database changes over time.
