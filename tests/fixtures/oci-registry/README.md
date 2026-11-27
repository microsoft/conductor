# Public OCI registry test fixtures

These credentials are public and must never be used outside tests. The server
certificate is signed by `ca.pem` and valid for `127.0.0.1` only. The CA private
key is not included. `htpasswd` is a bcrypt entry for `conductor-test` with
password `conductor-oci-test-password`.
