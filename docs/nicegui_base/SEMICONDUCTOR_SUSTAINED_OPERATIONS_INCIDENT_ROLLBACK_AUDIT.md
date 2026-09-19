# Semiconductor sustained-operations / incident / rollback evidence

Use Wave 73 only after Wave 72 has produced a rollback-readiness record and its exact deterministic ZIP.

1. Keep the Wave 72 JSON + ZIP immutable.
2. Import real sustained-operations evidence with `nicegui-base sustained-operations-accept`.
3. Treat requested ACCEPTED without current artifact bytes, authority/reference, framework identity, and exact NiceGUI identity as PENDING.
4. Close the documentary incident/rollback audit with `nicegui-base incident-rollback-audit-close` only against the exact same Wave 72 readiness identity and accepted sustained-operations record.
5. Package PENDING records for evidence-gap handoff when useful; do not bypass BLOCKED evidence.

Required stable sustained-operations artifact classes are `sustained-operations-window` and `incident-audit-summary`. Required audit-closure artifact classes are `incident-audit-closure` and `rollback-audit-closure`.

`ACCEPTED` and `CLOSED` are evidence states. NiceGUI Base does not continuously monitor production, perform incident response, execute rollback, deploy, publish, or approve a company release.
