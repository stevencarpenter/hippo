# Optional dependency disposition, 2026-10-05

The optional Firefox, site, and observability dependency audits remain non-clean.
Verified patches remove reported vulnerabilities, but two JavaScript package
advisories and container advisories remain open. This evidence does not approve
a release or establish 1.0 acceptance.

The Firefox and site scans target their resolved lockfiles. Container scans
target the exact image digests selected by `otel/docker-compose.yml`, on
`linux/arm64` and `linux/amd64`. These surfaces are separate from the core Rust
daemon and Python brain release bundle. The evidence describes prepared source
and artifacts, not an assertion about currently deployed services.

## JavaScript advisories and resolved changes

| Package | Before | Prepared version | Minimum advisory fix | Evidence |
|---|---|---|---|---|
| image-size, through Firefox web-ext | 2.0.2 | 2.0.4 | 2.0.3 | [JXL/HEIF parser](https://github.com/advisories/GHSA-5p2g-fcmc-qvqq), [ICNS parser](https://github.com/advisories/GHSA-w3rx-r6r6-pgpr) |
| brace-expansion, site | 5.0.9 | 5.0.12 | 5.0.12 | [Expansion advisory](https://github.com/advisories/GHSA-q2hr-2g5m-vwhr) |
| devalue, site | 5.9.2 | 5.9.4 | 5.9.3 | [Parser advisory](https://github.com/advisories/GHSA-j22f-vq7h-c4qm) |
| DOMPurify, site | 3.4.15 | 3.4.16 | 3.4.16 | [Sanitizer advisory](https://github.com/advisories/GHSA-p98j-92pf-mc4p) |
| fast-uri, site | 3.1.7 | 3.1.8 | 3.1.8 | [URI advisory](https://github.com/advisories/GHSA-hrr3-gc8f-f4qj) |
| undici, site | 7.29.0 | 7.29.1 | 7.29.1 | [TLS validation advisory](https://github.com/advisories/GHSA-w293-vg96-wgc3) |

Firefox `package.json` raises web-ext from 10.6.0 to 10.7.0. That upstream tool
release selects addons-linter 10.13.0 and image-size 2.0.4 without an override.
The stale Bun lock described web-ext 10.1.0. It was regenerated from the canonical
npm lock, normalized by Bun, and checked for the same 343 unique package/version
pairs. Subsequent frozen-lock checks preserved all input hashes.

The site retains its existing advisory-floor override policy in
`site/pnpm-workspace.yaml`. The devalue constraint is `^5.9.3`; pnpm selected the
compatible patch 5.9.4. The other selected site versions equal the corresponding
minimum fix. Only the five affected package resolutions and associated override
floors changed. The seven devalue and ten undici findings are included in the
machine-readable advisory evidence, rather than inferred from one advisory link.

| Remaining package | Installed | Fixed release | Advisory | Original / adjusted severity | Verified reachability boundary |
|---|---|---|---|---|---|
| node-forge | 1.4.0 | None published | [GHSA-86w9-cpqp-85rv](https://github.com/advisories/GHSA-86w9-cpqp-85rv) | High, CVSS4 8.7 / Low hardening for inspected flows | Present through web-ext and adbkit. Vulnerable `key.verify` calls occur in adbkit's TCP USB bridge. Hippo's build/package flow and web-ext's Android helper do not construct that bridge. |
| http-cache-semantics | 4.2.0 | No verified fix | [GHSA-ch52-4w7c-c8xp](https://github.com/advisories/GHSA-ch52-4w7c-c8xp) | High, CVSS4 8.7 / Low hardening for configured static build | Astro 7.2.8 uses `storable` and `timeToLive` for build-time remote-image TTLs. The inspected importer does not call vulnerable `evaluateRequest` or `satisfiesWithoutRevalidation` paths. |

The adjusted severities apply only to the inspected execution paths. They do not
change the upstream advisory status. The adbkit library does contain calls to
the vulnerable verifier; invoking its separate TCP USB bridge is outside the
evaluated Hippo flow. The built Firefox ZIP contains no `node_modules`, and its
three generated browser scripts contain no node-forge or image-size code.

http-cache-semantics 4.3.0 was inspected because it exists in the registry. Its
published source still reproduces the reported stale-cache reuse behavior.
The scan's `last_affected` boundary is insufficient evidence that 4.3.0 fixes it.
The dependency therefore remains at 4.2.0 with the advisory recorded as open.

OSV reports changed from 3 to 1 underlying package/advisory occurrences in the
Firefox npm lock, 56 to 1 in its stale Bun lock, and 23 to 1 in the site lock.
Native npm and pnpm audits confirm the remaining advisory in each ecosystem.
npm's three post-change High entries are one node-forge advisory and its two
parent dependency meta-vulnerabilities. No scanner exclusion was added.

## Observability image evidence

The selected image references use registry-verified multi-platform index
digests. Digest pins identify image content; they do not repair vulnerable code.
Prometheus, Grafana, Tempo, and Loki use verified maintenance-branch security
patches. Compose normalization preserves all non-image settings.

Prometheus 3.11.3 addresses the configured server's
[remote-read memory exhaustion](https://github.com/prometheus/prometheus/security/advisories/GHSA-8rm2-7qqf-34qm)
and [UI injection](https://github.com/prometheus/prometheus/security/advisories/GHSA-vffh-x6r8-xx99)
findings. Grafana 12.4.4 addresses
[request parsing](https://grafana.com/security/security-advisories/cve-2026-42127/),
[Loki traversal](https://grafana.com/security/security-advisories/cve-2026-42129/),
[datasource traversal](https://grafana.com/security/security-advisories/cve-2026-10601/),
and [Geomap injection](https://grafana.com/security/security-advisories/cve-2026-9029/).
The original High server findings are assessed as Medium for the checked-in
loopback-only host bindings; internal container access remains possible.

[Loki 3.7.8](https://github.com/grafana/loki/releases/tag/v3.7.8) is the first
3.7 patch documented to move gRPC to 1.83.2. The
[Tempo 2.10.8 security patch](https://grafana.com/docs/tempo/latest/release-notes/version-2/v2-10/)
updates its Go toolchain and dependencies. Neither patch is a claim that all
embedded packages are free of advisories.

Collector 0.150.1 is the published replacement for the unavailable 0.150.0
image. It removes 17 package/advisory pairs from 0.149.0, including the OTLP SDK
HTTP response-body and Go TLS/certificate findings, with no new pair in that
comparison. The unchanged Collector configuration passed its actual `validate`
command in a disposable container. [Collector release](https://github.com/open-telemetry/opentelemetry-collector-releases/releases/tag/v0.150.1),
[component changes](https://github.com/open-telemetry/opentelemetry-collector-contrib/releases/tag/v0.150.0).

| Image | Before | Prepared | Before / after occurrences per architecture | Remaining original Critical / High |
|---|---|---|---|---|
| Collector contrib | 0.149.0 | 0.150.1 | 102 / 85 | 3 / 49 |
| Tempo | 2.10.3 | 2.10.8 | 83 / 23 | 0 / 12 |
| Loki | 3.7.1 | 3.7.8 | 78 / 7 | 0 / 0 |
| Prometheus | 3.11.1 | 3.11.3 | 71 / 63 | 0 / 39 |
| Grafana | 12.4.2 | 12.4.4 | 269 / 169 | 1 / 48 |

Each occurrence is a distinct package/version/advisory within one image and
architecture. Duplicate binaries within an image are deduplicated. The summed
per-image count is 603 before and 347 after, separately for each architecture.
These totals are not globally unique vulnerabilities. The final inventory has
161 distinct advisory IDs across the images. Both architecture finding sets
match. Counts measure scanner inventory, not aggregate security risk.

Trivy 0.72.0 completed 22 initial and candidate artifact scans, using an advisory
database updated at 2026-10-05T07:14:49Z. Its exit 0 records successful scanning;
the invocation did not configure vulnerability findings to produce a failing
exit code. Every selected image still has findings.

| Remaining package | Installed | Dependency-level fix | Advisory | Original / adjusted severity | Reachability evidence |
|---|---|---|---|---|---|
| kin-openapi in Grafana | 0.133.0 | 0.144.0 | [GHSA-r277-6w6q-xmqw](https://github.com/getkin/kin-openapi/security/advisories/GHSA-r277-6w6q-xmqw) | Critical 9.1 / Critical retained | Module present. Grafana use of the vulnerable `ValidationHandler` with omitted `AuthenticationFunc` is unproven. This is not a demonstrated Grafana authentication bypass. |
| gRPC in Tempo | 1.82.1 | 1.83.1 for DATA frames; 1.82.2 or 1.83.2 for xDS | [CVE-2026-84304](https://github.com/grpc/grpc-go/security/advisories/GHSA-vp52-pcj8-j9qc), [CVE-2026-84445](https://github.com/grpc/grpc-go/security/advisories/GHSA-2v4p-qf9q-27wj) | High 8.7 / High for unqualified handler path; Low for unconfigured xDS | Internal gRPC exists. Tempo has no published host port. Function-level DATA-frame qualification is incomplete; xDS is not configured. |
| Go standard library / x/net in Collector, Tempo, Prometheus, Grafana | Go 1.26.2, 1.26.5, 1.26.2, 1.26.3 respectively; x/net 0.52.0 in Collector/Prometheus and 0.55.0 in Grafana | Go 1.26.6; x/net 0.56.0 for this DNS advisory | [CVE-2026-46600](https://pkg.go.dev/vuln/GO-2026-5942) and other inventory entries | High / High retained where reachability is unknown | Binary versions are established. HTTP, DNS, certificate and parser paths differ by service. The Tempo/Grafana patches introduce this reported DNS finding while removing older findings. |
| RabbitMQ amqp091-go in Collector | 1.10.0 | 1.13.0 | [CVE-2026-77405](https://github.com/rabbitmq/amqp091-go/security/advisories/GHSA-33mj-cw25-m34h), [CVE-2026-77408](https://github.com/rabbitmq/amqp091-go/security/advisories/GHSA-j497-x9hr-x34x), [CVE-2026-77411](https://github.com/rabbitmq/amqp091-go/security/advisories/GHSA-c5pq-fr2g-9jpf) | Critical, up to 9.4 / Low hardening | The checked-in Collector configuration enables no RabbitMQ receiver or exporter. These embedded modules are inactive under that configuration. |

The full residual inventory retains every architecture-specific row, installed
and fixed version, original and adjusted severity, CVSS vector, binary location,
and reachability disposition. Unresolved call paths retain the upstream
severity. Dependency-level fixes do not establish that a compatible fixed image
exists. No unsupported replacement of libraries inside an upstream image was
made. `govulncheck` was unavailable; no Go function-level call-graph clearance is
claimed.

The Collector validator ran on arm64 with no network, a read-only filesystem,
all capabilities dropped, and `no-new-privileges`. Its only bind mount was a
read-only private copy of the checked-in configuration. It exited 0 in 0.6
seconds, and cleanup confirmed no remaining fixture container. The source and
copy shared SHA-256
`83f013317fd0bb7d7995ef2a64ee16738cb4e89456859635f18d1e7eed499195`.
This proves configuration parsing for that artifact, not workload behavior,
data migration, performance, or rollback. No production container was restarted
or production data mounted by the dependency audit. Other image parser/runtime
checks are outside this audit's proof.

## Hygiene and evidence boundaries

1. Application lockfiles exist for both optional JavaScript ecosystems. Firefox
   locks now agree. Its only npm `hasInstallScript` entry is esbuild 0.28.1.
   Local verification installed with scripts disabled and used the locked
   platform binary. Normal npm installation can execute esbuild's install
   script; that capability alone is not evidence of malicious code.
2. Existing site override explanations were read before changing their floors.
   The existing Astro/markdown-remark and astro-pagefind peer warnings remain;
   local static validation passed. No unrelated direct dependency upgrade was
   introduced.
3. Grafana retains anonymous Admin and its existing default password behavior.
   Host bindings use loopback; the Compose network is still shared. Loopback
   does not establish an authentication boundary against local processes.
4. Registry provenance, integrity values, and image manifests were checked.
   Publisher ownership, build attestations, unknown vulnerabilities, vendored
   Readability, and the complete upstream build chains are not cleared by this
   audit. No maintainer-reputation conclusion is inferred from registry origin.
5. Graph coverage metadata was stale or absent for several lockfiles and
   changed configuration files. Direct current-source reads, resolved package
   inspection, native scanners, and executed consumers provide the proof.
   No graph-only exhaustive claim was made.

## Validation commands and results

Firefox verification used Node 24.21.0 and Bun 1.3.14. Its 13 tests passed;
TypeScript, bundling, and canonical XPI packaging passed. The generated ZIP
contains the final normalized Bun lock. Site verification used pnpm 10.20.0 and
Node 26.10.0: frozen installation passed, Astro checked 61 files with zero
diagnostics, 32 tests passed, and the static build passed. Hosted site CI uses
Node 24; these local results do not substitute for that separate CI run.

```sh
osv-scanner --version
osv-scanner scan source --lockfile extension/firefox/package-lock.json --format json
osv-scanner scan source --lockfile extension/firefox/bun.lock --format json
osv-scanner scan source --lockfile site/pnpm-lock.yaml --format json
mise exec node@24.21.0 -- npm --prefix extension/firefox audit --json
mise exec node@24.21.0 -- npm --prefix extension/firefox ci --ignore-scripts --no-audit
mise exec node@24.21.0 -- mise run build:ext
bun test extension/firefox/tests/
npx -y pnpm@10.20.0 --dir site install --frozen-lockfile
npx -y pnpm@10.20.0 --dir site exec astro check
npx -y pnpm@10.20.0 --dir site test
npx -y pnpm@10.20.0 --dir site build
npx -y pnpm@10.20.0 --dir site audit --json
docker compose -f otel/docker-compose.yml config --quiet
git diff --check
trivy --version
trivy image --download-db-only --no-progress --timeout 2m
command -v govulncheck
```

The lockfile scanners return exit 1 for the documented open advisories. A
successful build is compatibility evidence, not a vulnerability waiver.
Lock migration used `bun install --lockfile-only --ignore-scripts`, followed by
`bun install --frozen-lockfile --lockfile-only --ignore-scripts` in a private
directory containing copies of the updated npm files. The final three file
hashes remained stable after normalization.

Container scans used `trivy image --image-src remote --platform linux/arm64`
or `linux/amd64`, with `--scanners vuln --skip-db-update --skip-java-db-update
--no-progress --parallel 2 --timeout 3m --format json`. Each call specified the
complete verified tag plus index digest and a private output destination.
`docker buildx imagetools inspect` resolved every selected tag and its platform
manifests. The private command ledger preserves all expanded argv, output paths,
and the exact isolated Docker validator invocation. Compose normalization
compared the previous and prepared effective configurations and found only
image-reference changes.

## Evidence fingerprints

Raw evidence is retained outside Git with directory mode 0700 and file mode
0600. The current source references are bound by the checked-in lockfiles and
image digests. `otel/docker-compose.yml` has SHA-256
`875e3280ee658f49f21e1d17b814d252e79f6d605a12b2f51cf1bc16d2eab68f`.

| Private evidence file | SHA-256 |
|---|---|
| firefox-advisories.csv | `66438dd5e1b634a6469275ef5be2ed4ff2869da3f79b6a98731dbeb8846a524e` |
| firefox-npm-osv-after.json | `8af72729702c00d42256dfb0d7eb053b84b05fef7cec891c2d4153d4d874f99c` |
| firefox-bun-osv-after.json | `fed8c4a5357ec09425792bbd9fb9b98bef8c1c0dbe2a34efba52a4a77d66e613` |
| site-fix/site-osv-after.json | `3b5ec4ce658573f58beafee421178667a2d279f4862ddf82b0b4e2e2a58d0fd7` |
| containers/current-advisories.json | `6f1079b1bf6dcf9452d1dd5c3402a94f6a3f3394bf3f8069d1c171da6014f196` |
| containers/evidence-manifest.json | `14b318be27f99a32eea577f09b80d4e967a82d0bcf207da3a74d83669d58f92a` |
| containers/commands.txt | `1040a7ee6419ac7c79cfdaa83a2ac2dfea6621c78778fd8d86973770c5e58772` |

The container evidence manifest binds the per-image scan JSON, registry
manifests, source configuration, validation results, and full residual tables.
The JavaScript reports retain all original advisory IDs and the targeted
resolution changes.
