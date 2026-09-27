# EPHI synthetic downstream qualification package

This separate distribution installs the existing synthetic downstream ABI
fixture for bounded integration and browser qualification. It contains only
fictional identifiers and values. It has no company data, secrets, private
mappings, endpoints, or production SDKs.

Install it explicitly beside a matching EPHI release from the prepared
qualification wheelhouse. It is not an EPHI runtime dependency and is never
auto-selected. The supported provider entrypoint remains
`examples.synthetic_downstream.provider:build_bundle`, loaded and composed
through the public `org.ephi.downstream` ABI 1.0.0 authority.

The fixture does not qualify real-family G02/G06, production-like G10, real
independently audited G11, G12, the Port Gate, release promotion, or
Production.
