"use strict";
// Sign-in forms that talk directly to the Amazon Cognito API over HTTPS.
// The ID token goes to the gateway once, which checks it and sets its own session cookie.
// No token is kept in the browser.

const ERROR_MESSAGES = {
  NotAuthorizedException: "Incorrect email or password.",
  UsernameExistsException: "An account with this email already exists. Please sign in.",
  InvalidPasswordException: "Please use at least 10 characters, with an uppercase letter, a lowercase letter and a number.",
  CodeMismatchException: "That code is not correct. Please check it and try again.",
  ExpiredCodeException: "That code has expired. Please ask for a new one.",
  LimitExceededException: "Too many attempts. Please wait a while and try again.",
  TooManyRequestsException: "Too many attempts. Please wait a while and try again.",
  TooManyFailedAttemptsException: "Too many attempts. Please wait a while and try again.",
  CodeDeliveryFailureException: "We could not send the code to that email address.",
  PasswordResetRequiredException: "Please reset your password to continue.",
};
const GENERIC_ERROR = "Something went wrong. Please try again.";

const byId = (id) => document.getElementById(id);

async function loadConfig() {
  const response = await fetch("/static/auth-config.json", { cache: "no-cache" });
  if (!response.ok) throw new Error("Sign-in is not available right now.");
  return response.json();
}

async function callCognito(config, action, body) {
  const response = await fetch(`https://cognito-idp.${config.region}.amazonaws.com/`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": `AWSCognitoIdentityProviderService.${action}`,
    },
    body: JSON.stringify({ ClientId: config.clientId, ...body }),
  });
  const data = await response.json().catch(() => ({}));
  if (response.ok) return data;
  const error = new Error(GENERIC_ERROR);
  error.code = String(data.__type || "").split("#").pop();
  throw error;
}

// Only a path on this site, so a crafted link cannot send people elsewhere after sign-in.
function nextPath() {
  const next = new URLSearchParams(location.search).get("next") || "";
  return /^\/(?![\/\\])/.test(next) ? next : "/account";
}

async function startSession(idToken) {
  const response = await fetch("/api/session", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id_token: idToken }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : GENERIC_ERROR);
}

function setMessage(id, text) {
  const element = byId(id);
  element.textContent = text;
  element.hidden = !text;
}

function showView(name) {
  for (const view of document.querySelectorAll(".auth-view")) view.hidden = view.id !== `view-${name}`;
  setMessage("auth-error", "");
  setMessage("auth-status", "");
  byId(`view-${name}`).querySelector("input").focus();
}

function initSignIn(config) {
  // Held only until the redirect, to sign in right after a code is confirmed.
  let email = "";
  let password = "";

  function showError(error) {
    setMessage("auth-status", "");
    setMessage("auth-error", ERROR_MESSAGES[error.code] || error.message || GENERIC_ERROR);
  }

  async function signIn() {
    const { AuthenticationResult: result } = await callCognito(config, "InitiateAuth", {
      AuthFlow: "USER_PASSWORD_AUTH",
      AuthParameters: { USERNAME: email, PASSWORD: password },
    });
    if (!result) throw new Error(GENERIC_ERROR);
    await startSession(result.IdToken);
    location.replace(nextPath());
  }

  function showConfirm() {
    byId("confirm-email").textContent = email;
    showView("confirm");
  }

  function showReset() {
    byId("reset-email").textContent = email;
    showView("reset");
  }

  function onSubmit(id, handler) {
    const form = byId(id);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const submit = form.querySelector('[type="submit"]');
      submit.disabled = true;
      setMessage("auth-error", "");
      try {
        await handler();
      } catch (error) {
        showError(error);
      } finally {
        submit.disabled = false;
      }
    });
  }

  function onResend(id, action) {
    byId(id).addEventListener("click", async () => {
      setMessage("auth-error", "");
      try {
        await callCognito(config, action, { Username: email });
        setMessage("auth-status", "We sent a new code.");
      } catch (error) {
        showError(error);
      }
    });
  }

  for (const button of document.querySelectorAll("[data-view]")) {
    button.addEventListener("click", () => {
      const current = button.closest(".auth-view").querySelector('input[type="email"]');
      if (current?.value) email = current.value.trim();
      const next = byId(`view-${button.dataset.view}`).querySelector('input[type="email"]');
      if (next) next.value = email;
      showView(button.dataset.view);
    });
  }

  onSubmit("view-signin", async () => {
    email = byId("signin-email").value.trim();
    password = byId("signin-password").value;
    try {
      await signIn();
    } catch (error) {
      if (error.code !== "UserNotConfirmedException") throw error;
      await callCognito(config, "ResendConfirmationCode", { Username: email });
      showConfirm();
    }
  });

  onSubmit("view-signup", async () => {
    email = byId("signup-email").value.trim();
    password = byId("signup-password").value;
    await callCognito(config, "SignUp", {
      Username: email,
      Password: password,
      UserAttributes: [{ Name: "email", Value: email }],
    });
    showConfirm();
  });

  onSubmit("view-confirm", async () => {
    await callCognito(config, "ConfirmSignUp", {
      Username: email,
      ConfirmationCode: byId("confirm-code").value.trim(),
    });
    await signIn();
  });

  onSubmit("view-forgot", async () => {
    email = byId("forgot-email").value.trim();
    await callCognito(config, "ForgotPassword", { Username: email });
    showReset();
  });

  onSubmit("view-reset", async () => {
    password = byId("reset-password").value;
    await callCognito(config, "ConfirmForgotPassword", {
      Username: email,
      ConfirmationCode: byId("reset-code").value.trim(),
      Password: password,
    });
    await signIn();
  });

  onResend("confirm-resend", "ResendConfirmationCode");
  onResend("reset-resend", "ForgotPassword");
}

async function startSignIn() {
  const account = await fetch("/api/account", { cache: "no-store" }).catch(() => null);
  if (account?.ok) {
    location.replace(nextPath());
    return;
  }
  try {
    initSignIn(await loadConfig());
  } catch (error) {
    setMessage("auth-error", error.message);
  }
}

startSignIn();
