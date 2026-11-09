# AcadAdviserAI

The public company website. The home page (`/`) only presents the company and has no form. Student
profiles are entered on a separate page, `/advisor`, which returns a graduate program list.

```
Internet → load balancer (HTTPS) → gateway → ProgramSearchAgent → StudentNormalizer
                                      └─────→ SchoolInfoProcessor ←┘ (graph runtime only)
```

On AWS (`infra/`), only the load balancer faces the internet and only the gateway sits behind it. The
other services are private, and each accepts traffic only from its callers. The gateway exposes a small
allowlisted API, so the unauthenticated internal service APIs are never public. Locally the same
services run on loopback ports.

| Service | Role on this site |
|---|---|
| StudentNormalizer (SN) | Turns the resume or typed profile into a structured profile |
| ProgramSearchAgent (PSA) | Searches the web and writes the recommendation list |
| SchoolInfoProcessor (SIP) | Adds catalog facts to results it can match. The gateway reads it. PSA's default `responses` runtime never calls SIP; the opt-in graph runtime reads SIP evidence |

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

Searching needs a signed-in account, so set `COGNITO_USER_POOL_ID` and `COGNITO_CLIENT_ID` first. The app
client must allow `USER_PASSWORD_AUTH` and have no client secret.

## Accounts and plans

Visitors sign in with Amazon Cognito before they can search. The browser calls Cognito directly, then sends
the ID token once to `POST /api/session`. The gateway checks its signature, issuer, audience, expiry and
verified email, and sets its own httpOnly `aa_account` cookie for 30 days. Searches belong to the
Cognito `sub`.

| Plan | What it allows |
|---|---|
| Free | One search per mailbox (`jane+1@gmail.com` and `j.ane@gmail.com` count as one), with `FREE_FOLLOW_UPS` refinements. Free searches count toward the per-IP daily limit |
| Unlimited, $99 per year | Searches up to `SEARCHES_PER_USER_PER_DAY` (fair use), with `FOLLOW_UPS_PER_SEARCH` refinements each. No per-IP limit |

Without a plan, the API answers 402 and the page links to `/account`. A failed upstream call gives the free
search back. `MODEL_RUNS_PER_DAY` still caps the whole site, so raise it as subscribers grow.
The prices on the home page and in the terms are written in the HTML: update them along with `PLAN_PRICE_LABEL`.

Plan state lives in the tables `users`, `sessions`, `free_searches`, `subscriptions` and `stripe_events`:
on AWS in the `acadadviser` Postgres database (set by `DB_HOST`), locally in `data/gateway/gateway.sqlite3`.
Stripe is the record of truth for payments. To run the tests on Postgres, point
`ACADADVISER_TEST_DATABASE_URL` at a throwaway database; each test wipes it.

The old static S3 preview (`scripts/build_preview.py` and its `PreviewStack`) was removed: its pages
cannot reach the gateway's session API.

### Billing with Stripe

The gateway creates Stripe Checkout and Customer Portal sessions; card details only ever go to Stripe.
`POST /api/stripe/webhook` verifies the `Stripe-Signature` header and applies each event once.
Access follows the subscription status: `active`, `trialing` and `past_due` (while Stripe retries the card) allow searches.

Set up in the Stripe dashboard, in test mode first:

1. **Product and price:** one product, "AcadAdviserAI Unlimited", with a recurring yearly price of $99 USD.
   Put its `price_...` id in `STRIPE_PRICE_ID`.
2. **Stripe Tax:** turn it on, set the business address, and give the product the tax code for
   software as a service. Add state registrations as Stripe reports that you reach thresholds.
3. **Customer portal** (Settings → Billing → Customer portal): allow canceling at the end of the period,
   updating the payment method and viewing invoices. Turn off plan switching.
4. **Emails** (Settings → Billing → Subscriptions and emails): send receipts, failed-payment emails and
   **upcoming renewal reminders**. The terms promise a reminder before each yearly renewal.
5. **Retries:** turn on Smart Retries, and after the last retry set the subscription to **cancel**.
   That ends the `past_due` grace period.
6. **US only:** add the Radar rule `Block if :card_country: != 'US'`. The webhook also logs any
   checkout with a non-US billing address.
7. **Webhook endpoint:** `https://<domain>/api/stripe/webhook` with the events
   `checkout.session.completed` and `customer.subscription.created`, `.updated` and `.deleted`.
   Put its signing secret in `STRIPE_WEBHOOK_SECRET`.

Test locally with the Stripe CLI and card `4242 4242 4242 4242`:

```bash
stripe listen --forward-to 127.0.0.1:9080/api/stripe/webhook   # prints the whsec_ secret for .env
```

Test clocks in the dashboard simulate a renewal a year later and a failed card.

### Launch checklist for payments

- Have a lawyer review `terms.html` and `privacy.html`: automatic renewal (California and other states),
  the 14-day refund promise, the 18+ purchase rule, and minors using the service.
- Run one full test-mode purchase, renewal (test clock), cancellation and refund on the deployed site.
- Switch to `sk_live_` keys, a live price id and a live webhook secret. `stack.py doctor` warns until you do.
- Refunds are made in the Stripe dashboard. To refund and end the plan, cancel the subscription there too.

## Deploy to AWS

`infra/` holds one CDK stack, `AcadAdviserStack` (us-east-1, account 097771562795):

| Part | What it is |
|---|---|
| Network | VPC over 2 zones, 1 NAT gateway, private subnets for everything but the load balancer |
| Gateway | Fargate (ARM64, 0.25 vCPU / 0.5 GB) behind an HTTPS load balancer. HTTP redirects to HTTPS |
| PSA, SN, SIP | Private Fargate services (0.5 vCPU / 1 GB each), found at `<name>.acadadviser.internal` |
| Database | RDS Postgres 16 (`db.t4g.micro`, encrypted, 7-day backups, deletion protection). One database and login per service |
| PSA files | EFS, encrypted, with automatic backups. Kept if the stack is destroyed |
| Sign-in | Cognito user pool and app client (kept if the stack is destroyed) |
| Secrets | The existing `OPENAI_API_KEY` secret, a Stripe secret this stack creates, and one generated DB login per service |
| Alerts | Email to `contactEmail` on gateway errors, an unreachable gateway, or spend over 80% of `monthlyBudgetUsd` |

Settings live in `infra/cdk.json`. `gatewaySettings` passes extra gateway settings, such as the spend guards.
Expect roughly $120–150 a month before OpenAI usage: NAT gateway, load balancer, RDS and four small tasks.

PSA must stay at one task: it keeps each running search in memory and streams it to the gateway. Its
deploys stop the old task before starting the new one, so a search running during a deploy is lost.

### First deploy

Needs Docker running and the sibling repos (`ProgramSearchAgent`, `StudentNormalizer`,
`SchoolInfoProcessor`) next to this one. Images are built from their working trees, uncommitted changes included.

```bash
cd infra && npm install
CDK_DEFAULT_ACCOUNT=097771562795 npx cdk deploy AcadAdviserStack
```

The first deploy takes about 20–30 minutes, mostly RDS. Then:

1. **Alerts:** confirm the AWS Notifications subscription email sent to `contactEmail`.
2. **DNS at Namecheap:** point `ALIAS @` and `CNAME www` at the `LoadBalancerDns` output. Keep the Google
   MX and TXT records. Add the three `DkimRecord` outputs as CNAMEs.
3. **Sign-in email:** while `cognitoFromEmail` is empty, Cognito sends from its own address, limited to about
   50 emails a day. Once SES shows the domain as verified, request SES production access in the console,
   then set `cognitoFromEmail` (for example `no-reply@acadadviser.com`) in `cdk.json` and deploy again.
4. **Stripe:** follow "Billing with Stripe" above, with the webhook at the `StripeWebhookUrl` output. Then store the keys
   and restart the gateway so it reads them:

   ```bash
   aws secretsmanager put-secret-value --secret-id <StripeSecretName output> --secret-string \
     '{"STRIPE_SECRET_KEY":"sk_test_...","STRIPE_WEBHOOK_SECRET":"whsec_...","STRIPE_PRICE_ID":"price_..."}'
   aws ecs update-service --cluster acadadviser --service acadadviser-gateway --force-new-deployment
   ```

   Until the keys are set, the account page says subscriptions are not open yet.
5. **Check:** on a phone with Wi-Fi off, open `https://acadadviser.com`, create an account, and run the free search.

Later deploys run the same `cdk deploy` and rebuild whatever images changed.

### Tearing it down

`npx cdk destroy AcadAdviserStack` stops on the database until you turn off its deletion protection in the RDS
console. A destroy leaves a final RDS snapshot, the EFS file system and the Cognito user pool. These hold
customer data, so delete them yourself only when you mean to.

## Running on this Mac

`scripts/stack.py`, `deploy/Caddyfile` and the launchd files still run the whole site on this Mac for testing.
They are no longer the way to host it publicly.

## Operating notes

- **Deleting a student's data:** the privacy notice promises deletion on request, but there is no tool
  yet. It is a manual removal from the `student_normalizer` database, PSA's files on EFS, and the gateway's
  `users`, `sessions`, `searches` and `free_searches` rows. Delete the Cognito user too.
- **Backups:** RDS keeps 7 days of automated backups, and AWS Backup covers the EFS file system daily.
  Stripe holds the payment records.
- **Logs:** CloudWatch log groups `/acadadviser/<service>`, kept for a month.
- **Capacity:** at most 3 profile uploads run at once per gateway task. Later visitors see a busy message.
- **Legal:** the privacy notice and terms are plain-language drafts. Have them reviewed, especially
  if you serve minors or people in the EU or China.
