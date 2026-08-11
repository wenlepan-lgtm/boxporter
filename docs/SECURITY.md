# Security model

BoxPorter coordinates trusted local processes that share a filesystem. It is not an
authorization boundary and must not be exposed as a network service without an
additional authentication layer.

Recommendations:

- keep `.boxporter/` out of public repositories when reports may contain customer data;
- use least-privilege agent credentials;
- configure commands as argument arrays, not shell strings;
- review task files as untrusted input before giving an agent destructive tools;
- place pending, active, and passed boxes on the same filesystem for atomic renames;
- run secret scanning before publishing evidence;
- never treat an executor's self-reported success as an independent review.

Please report vulnerabilities through a private GitHub security advisory rather than a
public issue.
