# Version-sensitive lookups

Read this file only when the answer may differ across releases or deployment types.

## Keep the version axes separate

- **Automation 360 release**: product releases such as `v.32`, `v.39`, or `v.43`.
- **REST API generation**: endpoint families such as `/v1`, `/v2`, `/v3`, and `/v4`. These are not Automation 360 release numbers.
- **Package SDK release**: SDK artifacts have their own versions and compatibility notes.
- **Legacy product line**: Enterprise 10/11 and previously released Automation 360 documentation can remain searchable beside current material.
- **Deployment**: Cloud and On-Premises instructions can differ even when the product name is identical.

## Resolve ambiguity

1. Include the user's exact product release, API path, SDK version, and Cloud/On-Premises qualifier in the search query when provided.
2. Treat `version=v-2019` metadata as a publication-family label, not proof that a page is current or compatible with a specific Automation 360 release.
3. Prefer current `Automation 360` or `Control Room APIs` publications. Use previously released or Enterprise 11 publications only when the user requests them or the current docs explicitly route there.
4. Check page text for `deprecated`, `EoL`, `supported as of`, `replaced by`, and explicit release prerequisites.
5. If two official pages conflict, report both page dates and scopes. Do not resolve the conflict by assuming the numerically larger API generation is universally supported.
6. If the request omits a version and the answer materially varies, answer for the current documentation and state that assumption; ask for the installed release only when it would change the action the user should take.
