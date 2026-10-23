# Security

## Reporting a vulnerability

Please report security problems **privately**, not in a public issue:

- On GitHub: **Security → Report a vulnerability** on this repository, or
- By email: **gitcotech2@gmail.com** (subject starting with "Framewise security").

Include what you found, how to reproduce it, and the Framewise version
(Settings → About). You'll get a reply within a week. Once a fix is released,
you'll be credited in the release notes if you'd like.

## Supported versions

Only the latest release gets security fixes. Framewise updates itself through
Ubuntu's Software Updater, or with **Update Now** in Settings → About.

## How updates are protected

Packages in the Framewise APT repository
(<https://lnpaulin.github.io/framewise/apt/>) are signed with the Framewise
archive key:

```
F69B 9995 8DF3 DE44 9A33  CABF 2B8D 94D1 92EA DC4F
```

The key is installed as `/etc/apt/keyrings/framewise.gpg` and trusted for this
repository only (`Signed-By`). apt refuses packages that aren't signed with it.

## What Framewise can access

Everything runs on your computer and nothing is uploaded. The only network
request Framewise makes on its own is a daily check of the latest version on
GitHub (Settings → About → Updates turns it off). Its GNOME Shell extension
answers only the Framewise app itself, which is checked on every call.
