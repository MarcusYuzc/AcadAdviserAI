"use strict";
// The account page: plan status, the Stripe checkout and billing portal, and sign-out.

const $ = (id) => document.getElementById(id);
const CONFIRM_TRIES = 20;
const CONFIRM_INTERVAL_MS = 2000;

function showError(message) {
  $("account-error").textContent = message || "";
  $("account-error").hidden = !message;
}

async function api(path, options) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const body = await response.json().catch(() => ({}));
  if (response.status === 401) {
    location.replace(`/signin?next=${encodeURIComponent("/account")}`);
    throw new Error("Please sign in.");
  }
  if (!response.ok) {
    throw new Error(typeof body.detail === "string" ? body.detail : "Something went wrong. Please try again.");
  }
  return body;
}

function postJson(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

function formatDate(seconds) {
  return new Date(seconds * 1000).toLocaleDateString("en-US", { year: "numeric", month: "long", day: "numeric" });
}

function render(account) {
  $("account-email").textContent = account.email;
  for (const node of document.querySelectorAll("[data-price]")) node.textContent = account.price;
  $("plan-subscribed").hidden = !account.subscribed;
  $("plan-free").hidden = account.subscribed;
  for (const node of document.querySelectorAll("[data-follow-ups]")) {
    node.textContent = String(account.follow_ups_per_search);
  }

  if (account.subscribed) {
    const date = account.current_period_end ? formatDate(account.current_period_end) : null;
    let renewal = "Your subscription is active.";
    if (account.status === "past_due") renewal = "Your last payment did not go through. Please update your card.";
    else if (date && account.cancel_at_period_end) renewal = `Your subscription ends on ${date} and will not renew.`;
    else if (date) renewal = `Renews on ${date}.`;
    $("plan-renewal").textContent = renewal;
  } else {
    $("free-status").textContent = account.free_search_available
      ? `You have one free search, with ${account.free_follow_ups} refinements. No card needed.`
      : "You have used your free search. Subscribe to keep searching.";
    $("subscribe").disabled = !account.billing_available;
    if (!account.billing_available) showError("Subscriptions are not open yet.");
  }
  $("account-loading").hidden = true;
  $("account-ready").hidden = false;
}

// Stripe confirms the payment by webhook, which can land a moment after the redirect back here.
async function confirmCheckout() {
  $("plan-confirming").hidden = false;
  for (let attempt = 0; attempt < CONFIRM_TRIES; attempt += 1) {
    const account = await api("/api/account");
    render(account);
    if (account.subscribed) {
      $("plan-confirming").hidden = true;
      history.replaceState(null, "", "/account");
      return;
    }
    $("plan-free").hidden = true;
    await new Promise((resolve) => setTimeout(resolve, CONFIRM_INTERVAL_MS));
  }
  $("confirming-note").textContent =
    "Your payment is still being confirmed. Please refresh this page in a minute. If your plan does not appear, write to us and we will sort it out.";
}

async function redirectTo(path, body, button) {
  button.disabled = true;
  showError("");
  try {
    const { url } = await postJson(path, body);
    location.assign(url);
  } catch (error) {
    button.disabled = false;
    showError(error.message);
  }
}

$("subscribe").addEventListener("click", () => {
  if (!$("renewal-consent").checked) {
    showError("Please confirm that the plan renews every year until you cancel.");
    return;
  }
  redirectTo("/api/billing/checkout", { renewal_consent: true }, $("subscribe"));
});

$("manage-billing").addEventListener("click", () => redirectTo("/api/billing/portal", {}, $("manage-billing")));

$("sign-out").addEventListener("click", async () => {
  await postJson("/api/session/sign-out").catch(() => {});
  location.assign("/");
});

async function start() {
  try {
    if (new URLSearchParams(location.search).get("checkout") === "success") await confirmCheckout();
    else render(await api("/api/account"));
  } catch (error) {
    $("account-loading").hidden = true;
    $("account-ready").hidden = false;
    showError(error.message);
  }
}

start();
