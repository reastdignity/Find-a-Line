# Secrets of the public site

compose.yaml gives each of these files to one service as a Docker secret
(`/run/secrets/<name>`, read by the app's settings): the `findaline_*` files to `web`,
`opentopography_api_key` to `worker`. Put the value alone in the file, no
quotes; the files are git-ignored and never enter the image.

| File | Value |
| --- | --- |
| `findaline_session_secret` | A long random string that signs the sign-in cookie: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `findaline_github_client_secret` | The client secret of the GitHub OAuth app |
| `findaline_google_client_secret` | The client secret of the Google OAuth client |
| `opentopography_api_key` | The OpenTopography API key for `--source nasadem` (admins only on the public site; free at [opentopography.org](https://portal.opentopography.org/myopentopo)). OpenTopography allows one user per key: your own key only when you are the site's only administrator, otherwise empty |

`deploy/secrets.example/` holds the four files empty: `cp -n deploy/secrets.example/* secrets/`,
then put the values in. Every file must exist (compose refuses to start without it): leave the file of a provider you do not
use empty (with its client id unset), and it is not offered; an empty `opentopography_api_key`
means no NASADEM. The session secret must have at least 32
characters. The container user (uid 10001) must be able to read it, e.g. `sudo chown 10001:10001 secrets/findaline_* secrets/opentopography_api_key && sudo chmod 400 secrets/findaline_* secrets/opentopography_api_key`.
