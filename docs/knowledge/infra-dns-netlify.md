# EnergyAgent infra: Netlify sites + yourenergyagent.com DNS (June 2026)

## Netlify (account: ford.genereaux@gmail.com, team ford-genereaux)

| Site | Project ID | URL | Source |
|---|---|---|---|
| solaroperator | af4d43ee-e74d-4e9a-9137-996a1fb71a3a | solaroperator.org | Garface111/solaroperator-site |
| energyagent-250 | 3435ee7a-3665-456b-b5e8-1e49dc45b9aa | yourenergyagent.com | Garface111/energyagent-site (~/energyagent-site) |
| array-operator-ea | 966cb1f5-944e-41fd-855b-10053edc5d18 | array-operator-ea.netlify.app | Garface111/array-operator (~/array-operator), publish dir `public/` |

- **Deploy `--site` wants the UUID, not the slug.** `netlify deploy --prod --dir public
  --site array-operator-ea` fails "Failed retrieving site data … Not Found" even when the
  repo is correctly linked. Use the Project ID column above: `netlify deploy --prod --dir
  public --site 966cb1f5-944e-41fd-855b-10053edc5d18`. The siteId is also in the repo's
  `.netlify/state.json`. Per-repo publish dir differs: array-operator deploys `--dir public`,
  the apex energyagent-site deploys `--dir .`.

- Custom domain attach via CLI (no UI needed):
  `netlify api updateSite --data '{"site_id":"<id>","body":{"custom_domain":"<apex>","domain_aliases":["www.<apex>"]}}'`
- Manual deploy: `cd <repo> && netlify deploy --prod --dir .`
- Netlify auth token for CI lives at `/root/.config/netlify/config.json` →
  `users.<id>.auth.token`. GitHub repo secrets NETLIFY_AUTH_TOKEN + NETLIFY_SITE_ID
  are set on energyagent-site.
- GitHub→Netlify CI workflow file exists locally (.github/workflows/deploy.yml) but is
  UNPUSHED: gh token lacks `workflow` scope. Fix: `gh auth refresh -s workflow -h github.com`
  (device-code flow, needs Ford). Until then deploy manually via CLI.

## yourenergyagent.com DNS (GoDaddy)

Correct final state:
- A @ → 75.2.60.5 (Netlify load balancer)
- CNAME www → energyagent-250.netlify.app

GoDaddy pitfalls hit during setup:
1. **"Record name www conflicts"** — GoDaddy pre-creates a www record on new domains.
   EDIT the existing row's value, don't add a second record.
2. **GoDaddy Website Builder / Airo auto-attaches A records** (76.223.105.230 and
   13.248.243.5) to the apex. Browsers round-robin across all A records, so the parked
   "Energy Agent — Empowering Energy Efficiency" placeholder (Playfair Display font,
   Getty stock image, generator meta "Go Daddy Website Builder") intermittently wins.
   Delete both rogue A records AND disconnect the Website Builder product or it can
   re-add them.
3. Netlify can't issue the apex Let's Encrypt cert until the rogue A records are gone
   (you'll see the GoDaddy DV cert being served instead — check `curl -sv ... | grep issuer`).

Verification one-liner:
```
getent hosts yourenergyagent.com   # must show ONLY 75.2.60.5
curl -sv https://yourenergyagent.com/ 2>&1 | grep issuer   # must be Let's Encrypt
curl -s https://yourenergyagent.com/ | grep -o "<title>[^<]*"   # EnergyAgent — AI agents...
```

## Brand rule

Brand is ALWAYS "EnergyAgent" in copy/UI/commits. The "your" prefix exists ONLY in the
domain (energyagent.com was taken — registered 2004, GoDaddy, someone else's).
Landing page: two doors — NEPOOL Verifiers → solaroperator.org/onboarding;
Array Owners → solaroperator.org/accounts (EARLY ACCESS badge).
Footer: "a Dyson Swarm Technologies product". Internal alerts go to
ford.genereaux@dysonswarmtechnologies.com (INTERNAL_ALERT_TO in api/notify.py).
