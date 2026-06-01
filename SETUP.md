# GitHub Profile README

Auto-generated OSS contribution list for [@kubeboiii](https://github.com/kubeboiii).

## Setup

1. Create this repo as **`kubeboiii/kubeboiii`** (public, same name as your username).
2. Push all files from this directory.
3. In repo **Settings → Actions → General**, set workflow permissions to **Read and write**.
4. Add a secret **`GH_PAT`** (Settings → Secrets → Actions):
   - Classic PAT with `read:user` and `public_repo`, or
   - Fine-grained token with read access to your account and public repos
5. Go to **Actions → Update OSS README → Run workflow** to generate the first `README.md`.

## Local regenerate

```bash
pip install -r requirements.txt
GH_TOKEN="$(gh auth token)" python3 scripts/generate_readme.py
```

## Files

| File | Purpose |
|------|---------|
| `README.template.md` | Static header — name, tagline; badges generated from config |
| `config/profile.config.yaml` | Theme, profile views, excludes, flagship repos, ecosystem tags |
| `scripts/generate_readme.py` | Fetches merged + open PRs, writes `README.md` |
| `.github/workflows/update-readme.yml` | Regenerates every 12 hours + manual trigger |

## Customize

- **Header / links:** edit `linkedin_url`, `website_url`, and `theme.badge_color` in config
- **Profile views:** komarev counter in header; set `profile_views.enabled: false` to disable
- **Badge color:** change `theme.badge_color` once to retheme every badge (header, stats, stars)
- **Hide your forks:** add repos to `exclude_repos` in config
- **Sort order:** change `sort_by` to `stars`, `pr_count`, or `recent`
