# Secrets of the public site

compose.yaml gives each of these files to the `web` service as a Docker secret
(`/run/secrets/<name>`, read by the app's settings). Put the value alone in the file, no
quotes; the files are git-ignored and never enter the image.

| File | Value |
| --- | --- |
| `findaline_session_secret` | A long random string that signs the sign-in cookie: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `findaline_github_client_secret` | The client secret of the GitHub OAuth app |
| `findaline_google_client_secret` | The client secret of the Google OAuth client |

Every file must exist (compose refuses to start without it), and the container
user (uid 10001) must be able to read it, e.g. `sudo chown 10001:10001 secrets/findaline_* && sudo chmod 400 secrets/findaline_*`.
