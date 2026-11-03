# AcadAdviserAI

The public company website. The home page (`/`) only presents the company and has no form. Student
profiles are entered on a separate page, `/advisor`, which returns a graduate program list.

```
Internet → Caddy (HTTPS, :80/:443) → gateway (:9080) → ProgramSearchAgent (:9003) → StudentNormalizer (:9000)
                                         └──────────→ SchoolInfoProcessor (:9001)
```

Only Caddy faces the internet. The gateway exposes a small allowlisted API, so the unauthenticated
internal service APIs stay on loopback.

| Service | Role on this site |
|---|---|
| StudentNormalizer (SN) | Turns the resume or typed profile into a structured profile |
| ProgramSearchAgent (PSA) | Searches the web and writes the recommendation list |
| SchoolInfoProcessor (SIP) | Adds catalog facts to results it can match. The gateway reads it, so PSA's "never calls SIP" rule still holds |

SchoolRecommender is not part of this site, so visitors get PSA's web list only (no catalog list A).

## Run locally

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env        # for local testing: COOKIE_SECURE=false
.venv/bin/python scripts/stack.py up
```

Open http://127.0.0.1:9080. Stop with Ctrl-C, or `pkill -f "stack.py up"` if it runs in the background.
Tests: `.venv/bin/pytest`. Logs are in `logs/`.

The stack uses its own ports and `data/psa`, so it does not touch dev services on 8000-8003 or
PSA's evaluation data. It shares the local SN and SIP Postgres databases.

## AWS preview (live now)

A static preview runs on S3 and CloudFront, so it stays up with this Mac off:
https://djjasa59otczz.cloudfront.net

It has the home page, sign-in and a signed-in account page. It stores no student data, and the profile form
is deliberately not published. `infra/` holds the CDK stack (`AcadAdviserPreviewStack`, us-east-1).

"Start my list" goes to `/signin.html`, the site's own sign-in and sign-up form, which calls the Amazon Cognito
API directly from the browser. It then goes to `/account.html`, which says program lists are opening soon.
Cognito's hosted page stays configured but unused, ready for Google sign-in later. There is nothing private on this static site, so
sign-in protects no data yet. When the form goes public, the backend must verify Cognito tokens itself.
Verification emails use Cognito's default sender, which is limited to about 50 emails a day; move to
Amazon SES before any real launch.

Update it after changing the site:

```bash
.venv/bin/python scripts/build_preview.py   # uses CONTACT_EMAIL from .env
cd infra && CDK_DEFAULT_ACCOUNT=097771562795 npx cdk deploy AcadAdviserPreviewStack
```

Remove it completely: `cd infra && npx cdk destroy AcadAdviserPreviewStack`.

### Domain

`acadadviser.com` and `www.acadadviser.com` are set in `infra/cdk.json`, using an ACM certificate that
renews automatically while its two validation CNAMEs stay at Namecheap. At Namecheap, `ALIAS @` and
`CNAME www` point to `djjasa59otczz.cloudfront.net`. Keep the MX and TXT records: they carry the Google email.

### Going functional on AWS

This preview cannot process profiles. That needs the gateway, SN, SIP and PSA running as containers with
a database, as a separate stack. PSA also keeps its state in local files, which must move to
durable storage first. The Mac and Caddy steps below are the alternative to that.

## Go live from this Mac

Run `scripts/stack.py doctor` at any point. It lists what is still missing.

### 1. Confirm inbound traffic can reach this Mac

A traceroute on 2026-11-02 showed `192.168.50.1 → 10.10.25.1 → public internet`. The second private
hop suggests **double NAT**, which would make a port forward on your router useless on its own.

Open your router's admin page (http://192.168.50.1) and read its WAN IP:

- **Matches your public IP** (76.8.94.233 when checked): forward ports 80 and 443 to this Mac.
- **Starts with 10., 192.168., 172.16-31. or 100.64-127.**: there is another NAT upstream. Ask the
  ISP or building for a public IP or for ports 80/443 to be forwarded. If they refuse, this
  network cannot host directly; see "If inbound is blocked".

Also check that your ISP plan allows running a server, and reserve this Mac's LAN IP on the router.

### 2. DNS

At the registrar, point `A @` and `A www` at your public IP. A home IP can change, so ask the ISP
for a static IP or use the registrar's dynamic DNS.

### 3. Production settings

In `.env`, set `SITE_DOMAIN`, `CONTACT_EMAIL`, `COOKIE_SECURE=true`, `PSA_AUTH_MODE=api_key`.
Review the spend guards too. Each search and follow-up is a paid model run.

PSA and SN need `OPENAI_API_KEY` in their own `.env` files. Set a monthly budget limit in the OpenAI
dashboard as a backstop. Do not serve the public from a personal ChatGPT plan.

Rebind both Postgres ports to loopback (they currently accept LAN connections with weak passwords):
in each `docker-compose.yml` use `"127.0.0.1:5432:5432"` / `"127.0.0.1:5433:5432"`, then `docker compose up -d`.

### 4. Start and survive reboots

```bash
pkill -f "stack.py up"       # stop any test stack first
cp deploy/com.acadadviser.*.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.acadadviser.stack.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.acadadviser.caddy.plist
```

Caddy gets HTTPS certificates automatically once DNS and ports work. Then, in macOS Settings:
- Allow Caddy through the firewall.
- Turn on automatic login (agents start at login), and open Docker Desktop at login.
- Keep the Mac plugged in with the lid open. `stack.py up` blocks idle sleep on AC power, but closing the lid still sleeps it.

### 5. Verify from outside

On a phone with Wi-Fi off, open `https://<your domain>` and run one search.

## If inbound is blocked

Keep everything on this Mac and add an outbound tunnel. Cloudflare Tunnel is the simplest, and has
a free tier. This is a small cloud dependency: Cloudflare decrypts traffic at its edge, so resumes
pass through it, and the privacy notice must say so. A small VPS running Caddy with a WireGuard link
back to this Mac avoids that, but it is a server you rent and maintain.

## Operating notes

- **Deleting a student's data:** the privacy notice promises deletion on request, but there is no tool
  yet. It is a manual removal from SN's Postgres tables and `data/psa`.
- **Backups:** none are configured. A disk failure loses everything on this Mac.
- **Capacity:** at most 3 profile uploads run at once. Later visitors see a busy message.
- **Legal:** the privacy notice and terms are plain-language drafts. Have them reviewed, especially
  if you serve minors or people in the EU or China.
