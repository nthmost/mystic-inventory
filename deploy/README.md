# Deploying mystic (zephyr)

Live at **https://mystic.nthmost.net**. Layout on zephyr:

```
/opt/mystic/app          git clone of this repo (git pull to update)
/opt/mystic/app/.venv    venv, `pip install -e .[web]`
/opt/mystic/data/index.db  central merged index (CRATE_DB)
/opt/mystic/mystic.env   EnvironmentFile (chmod 600, root) — secrets, NOT in git
```

`mystic.env` holds: `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`,
`MYSTIC_ALLOWED_USERS`, `MYSTIC_PUSH_TOKEN`, `MYSTIC_SECRET_KEY`,
`CRATE_DB=/opt/mystic/data/index.db`.

## Service + proxy
- systemd unit: [`mystic.service`](mystic.service) → `/etc/systemd/system/` (gunicorn on 127.0.0.1:8901)
- Apache vhost: [`apache-mystic.conf`](apache-mystic.conf) → `/etc/apache2/sites-available/mystic.nthmost.net.conf`, `a2ensite`, then `certbot --apache -d mystic.nthmost.net --redirect`.
- On the certbot-generated `*-le-ssl.conf`, set `X-Forwarded-Proto "https"`.

## Update
```sh
ssh zephyr 'cd /opt/mystic/app && git pull && ./.venv/bin/pip install -q -e ".[web]" && sudo systemctl restart mystic'
```

## GitHub OAuth app
Registered at github.com/settings/developers — callback `https://mystic.nthmost.net/auth/callback`,
homepage `https://mystic.nthmost.net`. Only `MYSTIC_ALLOWED_USERS` may sign in.

## Feeding the index (from any host)
```sh
echo https://mystic.nthmost.net > ~/.config/crate/server
echo <push-token>              > ~/.config/crate/push_token
crate scan …   # or crate volume scan …
crate push
```
