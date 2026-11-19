// Cognito login wall at CloudFront (viewer request), for n8n-on-aws.
//
// Lambda@Edge has no environment variables: static settings come from config.json,
// written at synth time, and the user pool and app client come from an SSM parameter
// in us-east-1, read once per cold start. The flow is the OAuth 2.0 code grant with
// PKCE on a public app client, so no client secret exists anywhere. The ID token is
// kept in an HttpOnly cookie and checked on every request against the pool's keys.
// Paths that external callers use (webhooks, forms, MCP) have their own CloudFront
// behaviors without this function.

import { createHash, createPublicKey, randomBytes, verify } from "node:crypto";
import { readFileSync } from "node:fs";
import { GetParameterCommand, SSMClient } from "@aws-sdk/client-ssm";

const config = JSON.parse(
  readFileSync(new URL("./config.json", import.meta.url), "utf8"),
);
const TOKEN = "n8n_edge_token";
const PKCE = "n8n_edge_pkce";

let settings = null; // {userPoolId, clientId, cognitoDomain, region}
let keys = null; // kid -> KeyObject

async function load() {
  if (settings === null) {
    const ssm = new SSMClient({ region: config.parameterRegion });
    const out = await ssm.send(new GetParameterCommand({ Name: config.parameter }));
    const value = JSON.parse(out.Parameter.Value);
    if (!value.userPoolId || !value.clientId) throw new Error("not filled in yet");
    settings = value;
  }
  if (keys === null) {
    const url = `${issuer()}/.well-known/jwks.json`;
    const response = await fetch(url);
    if (!response.ok) throw new Error(`JWKS ${response.status}`);
    const { keys: list } = await response.json();
    keys = Object.fromEntries(
      list.map((jwk) => [jwk.kid, createPublicKey({ key: jwk, format: "jwk" })]),
    );
  }
}

function issuer() {
  return `https://cognito-idp.${settings.region}.amazonaws.com/${settings.userPoolId}`;
}

const base = () => `https://${config.domain}`;
const redirectUri = () => base() + config.callbackPath;

function cookies(request) {
  const found = {};
  for (const header of request.headers.cookie || []) {
    for (const part of header.value.split(";")) {
      const index = part.indexOf("=");
      if (index > 0) found[part.slice(0, index).trim()] = part.slice(index + 1).trim();
    }
  }
  return found;
}

function withoutOurCookies(request) {
  const kept = [];
  for (const header of request.headers.cookie || []) {
    const parts = header.value
      .split(";")
      .map((p) => p.trim())
      .filter((p) => p && !p.startsWith(`${TOKEN}=`) && !p.startsWith(`${PKCE}=`));
    if (parts.length) kept.push({ key: "Cookie", value: parts.join("; ") });
  }
  if (kept.length) request.headers.cookie = kept;
  else delete request.headers.cookie;
  return request;
}

function claims(token) {
  const parts = (token || "").split(".");
  if (parts.length !== 3) return null;
  try {
    const header = JSON.parse(Buffer.from(parts[0], "base64url").toString());
    const key = keys[header.kid];
    if (header.alg !== "RS256") return null;
    if (!key) {
      keys = null; // an unknown key id: Cognito may have rotated its keys
      return null;
    }
    const signed = Buffer.from(`${parts[0]}.${parts[1]}`);
    if (!verify("RSA-SHA256", signed, key, Buffer.from(parts[2], "base64url"))) {
      return null;
    }
    const payload = JSON.parse(Buffer.from(parts[1], "base64url").toString());
    const valid =
      payload.iss === issuer() &&
      payload.aud === settings.clientId &&
      payload.token_use === "id" &&
      payload.exp > Date.now() / 1000;
    return valid ? payload : null;
  } catch {
    return null;
  }
}

function cookie(name, value, maxAge) {
  return {
    key: "Set-Cookie",
    value: `${name}=${value}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=${maxAge}`,
  };
}

function respond(status, headers, body) {
  const response = {
    status: String(status),
    headers: {
      "cache-control": [{ key: "Cache-Control", value: "no-store" }],
      ...headers,
    },
  };
  if (body !== undefined) response.body = body;
  return response;
}

function redirect(location, setCookies = []) {
  const headers = { location: [{ key: "Location", value: location }] };
  if (setCookies.length) headers["set-cookie"] = setCookies;
  return respond(302, headers);
}

function login(request) {
  const wantsPage =
    ["GET", "HEAD"].includes(request.method) &&
    (request.headers.accept || []).some((h) => h.value.includes("text/html"));
  if (!wantsPage) {
    return respond(
      401,
      { "content-type": [{ key: "Content-Type", value: "application/json" }] },
      JSON.stringify({ message: "sign in again: the session has expired" }),
    );
  }
  const verifier = randomBytes(32).toString("base64url");
  const state = randomBytes(16).toString("base64url");
  const target = request.uri + (request.querystring ? `?${request.querystring}` : "");
  const back = Buffer.from(target).toString("base64url");
  const query = new URLSearchParams({
    response_type: "code",
    client_id: settings.clientId,
    redirect_uri: redirectUri(),
    scope: "openid email",
    state,
    code_challenge: createHash("sha256").update(verifier).digest("base64url"),
    code_challenge_method: "S256",
  });
  return redirect(`https://${settings.cognitoDomain}/oauth2/authorize?${query}`, [
    cookie(PKCE, `${state}.${verifier}.${back}`, 600),
  ]);
}

async function callback(request) {
  const query = new URLSearchParams(request.querystring);
  const [state, verifier, back] = (cookies(request)[PKCE] || "").split(".");
  if (!query.get("code") || !state || query.get("state") !== state) {
    return respond(400, {}, "The sign-in could not be completed. Open n8n again.");
  }
  const response = await fetch(`https://${settings.cognitoDomain}/oauth2/token`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: settings.clientId,
      code: query.get("code"),
      redirect_uri: redirectUri(),
      code_verifier: verifier,
    }),
  });
  const tokens = response.ok ? await response.json() : {};
  let payload = claims(tokens.id_token);
  if (payload === null && keys === null) {
    await load(); // fresh keys, in case they were rotated
    payload = claims(tokens.id_token);
  }
  if (payload === null) {
    return respond(401, {}, "The sign-in was refused. Open n8n again.");
  }
  const target = Buffer.from(back || "", "base64url").toString();
  const maxAge = Math.max(
    0,
    Math.min(config.sessionSeconds, Math.floor(payload.exp - Date.now() / 1000)),
  );
  return redirect(base() + (target.startsWith("/") ? target : "/"), [
    cookie(TOKEN, tokens.id_token, maxAge),
    cookie(PKCE, "", 0),
  ]);
}

function signout() {
  const query = new URLSearchParams({
    client_id: settings.clientId,
    logout_uri: `${base()}/`,
  });
  return redirect(`https://${settings.cognitoDomain}/logout?${query}`, [
    cookie(TOKEN, "", 0),
  ]);
}

export async function handler(event) {
  const request = event.Records[0].cf.request;
  try {
    await load();
  } catch (error) {
    settings = null;
    keys = null;
    console.error("edge auth is not configured yet", error);
    return respond(503, {}, "n8n is starting. Try again in a minute.");
  }
  if (request.uri === config.callbackPath) return callback(request);
  if (request.uri === config.signoutPath) return signout();
  if (claims(cookies(request)[TOKEN]) !== null) return withoutOurCookies(request);
  return login(request);
}
