// Exercises the Lambda@Edge login wall with a stubbed SSM client and Cognito.
// Run by tests/test_edge_auth.py with the function copied next to this file.

import assert from "node:assert/strict";
import { createHash, generateKeyPairSync, sign } from "node:crypto";
import { state } from "@aws-sdk/client-ssm";

const POOL = "eu-west-3_Example";
const CLIENT = "client123";
const ISSUER = `https://cognito-idp.eu-west-3.amazonaws.com/${POOL}`;
const COGNITO = "n8n-test.auth.eu-west-3.amazoncognito.com";

const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
const jwk = { ...publicKey.export({ format: "jwk" }), kid: "k1", alg: "RS256" };
const other = generateKeyPairSync("rsa", { modulusLength: 2048 }).privateKey;

function token(claims = {}, key = privateKey) {
  const header = Buffer.from(JSON.stringify({ alg: "RS256", kid: "k1" })).toString(
    "base64url",
  );
  const payload = Buffer.from(
    JSON.stringify({
      iss: ISSUER,
      aud: CLIENT,
      token_use: "id",
      exp: Math.floor(Date.now() / 1000) + 3600,
      email: "me@example.com",
      ...claims,
    }),
  ).toString("base64url");
  const signature = sign("RSA-SHA256", Buffer.from(`${header}.${payload}`), key);
  return `${header}.${payload}.${signature.toString("base64url")}`;
}

const calls = [];
let issued = token();
globalThis.fetch = async (url, options = {}) => {
  calls.push({ url: String(url), options });
  if (String(url).endsWith("/.well-known/jwks.json")) {
    return { ok: true, json: async () => ({ keys: [jwk] }) };
  }
  if (String(url) === `https://${COGNITO}/oauth2/token`) {
    return { ok: true, json: async () => ({ id_token: issued }) };
  }
  return { ok: false, status: 404, json: async () => ({}) };
};

state.value = JSON.stringify({
  userPoolId: POOL,
  clientId: CLIENT,
  cognitoDomain: COGNITO,
  region: "eu-west-3",
});

const { handler } = await import("./index.mjs");

function event(uri, { cookie, accept = "text/html", method = "GET", qs = "" } = {}) {
  const headers = { host: [{ key: "Host", value: "n8n.example.com" }] };
  if (cookie) headers.cookie = [{ key: "Cookie", value: cookie }];
  if (accept) headers.accept = [{ key: "Accept", value: accept }];
  return { Records: [{ cf: { request: { uri, method, querystring: qs, headers } } }] };
}

const header = (response, name) => (response.headers[name] || []).map((h) => h.value);

// 1. A page without a session goes to Cognito with PKCE.
const first = await handler(event("/workflow/42", { qs: "a=1" }));
assert.equal(first.status, "302");
const authorize = new URL(header(first, "location")[0]);
assert.equal(authorize.host, COGNITO);
assert.equal(authorize.pathname, "/oauth2/authorize");
assert.equal(authorize.searchParams.get("client_id"), CLIENT);
assert.equal(
  authorize.searchParams.get("redirect_uri"),
  "https://n8n.example.com/_auth/callback",
);
assert.equal(authorize.searchParams.get("code_challenge_method"), "S256");
const pkce = header(first, "set-cookie")[0];
assert.match(pkce, /^n8n_edge_pkce=.*HttpOnly.*SameSite=Lax/);
const [stateValue, verifier, back] = pkce.split(";")[0].split("=")[1].split(".");
assert.equal(authorize.searchParams.get("state"), stateValue);
assert.equal(
  authorize.searchParams.get("code_challenge"),
  createHash("sha256").update(verifier).digest("base64url"),
);
assert.equal(Buffer.from(back, "base64url").toString(), "/workflow/42?a=1");

// 2. An API call without a session gets 401, not a redirect.
const api = await handler(event("/rest/workflows", { accept: "application/json" }));
assert.equal(api.status, "401");

// 3. A callback with the wrong state is refused, and no token is requested.
const before = calls.length;
const wrong = await handler(
  event("/_auth/callback", {
    qs: "code=abc&state=forged",
    cookie: pkce.split(";")[0],
  }),
);
assert.equal(wrong.status, "400");
assert.equal(calls.length, before);

// 4. A good callback exchanges the code with the verifier and sets the session.
const done = await handler(
  event("/_auth/callback", {
    qs: `code=abc&state=${stateValue}`,
    cookie: pkce.split(";")[0],
  }),
);
assert.equal(done.status, "302");
assert.equal(header(done, "location")[0], "https://n8n.example.com/workflow/42?a=1");
const exchange = calls.at(-1);
const form = new URLSearchParams(exchange.options.body);
assert.equal(form.get("grant_type"), "authorization_code");
assert.equal(form.get("code_verifier"), verifier);
assert.equal(form.get("client_id"), CLIENT);
const session = header(done, "set-cookie").find((c) => c.startsWith("n8n_edge_token="));
assert.match(session, /HttpOnly; SameSite=Lax; Max-Age=3[0-9]{3}$/);

// 5. A valid session passes; our cookies are not sent to n8n, others are.
const cookieValue = session.split(";")[0];
const passed = await handler(
  event("/rest/workflows", { cookie: `n8n-auth=xyz; ${cookieValue}` }),
);
assert.equal(passed.uri, "/rest/workflows");
assert.deepEqual(passed.headers.cookie, [{ key: "Cookie", value: "n8n-auth=xyz" }]);

// 6. Expired, foreign-audience, wrong-issuer, access and forged tokens are refused.
for (const bad of [
  token({ exp: Math.floor(Date.now() / 1000) - 10 }),
  token({ aud: "another-client" }),
  token({ iss: "https://cognito-idp.eu-west-3.amazonaws.com/eu-west-3_Other" }),
  token({ token_use: "access" }),
  token({}, other),
  "not.a.token",
]) {
  const refused = await handler(event("/", { cookie: `n8n_edge_token=${bad}` }));
  assert.equal(refused.status, "302", bad);
}

// 7. A refused token from Cognito does not open a session.
issued = token({ aud: "another-client" });
const [s2, v2] = (await handler(event("/")).then((r) => header(r, "set-cookie")[0]))
  .split(";")[0]
  .split("=")[1]
  .split(".");
const refusedCallback = await handler(
  event("/_auth/callback", {
    qs: `code=abc&state=${s2}`,
    cookie: `n8n_edge_pkce=${s2}.${v2}.`,
  }),
);
assert.equal(refusedCallback.status, "401");

// 8. A key rotation: an unknown key id reloads the keys on the callback.
const rotated = generateKeyPairSync("rsa", { modulusLength: 2048 });
const jwks = calls.filter((c) => c.url.endsWith("jwks.json")).length;
jwk.kid = "k2";
Object.assign(jwk, rotated.publicKey.export({ format: "jwk" }));
issued = (() => {
  const h = Buffer.from(JSON.stringify({ alg: "RS256", kid: "k2" })).toString(
    "base64url",
  );
  const p = Buffer.from(
    JSON.stringify({
      iss: ISSUER,
      aud: CLIENT,
      token_use: "id",
      exp: Math.floor(Date.now() / 1000) + 3600,
    }),
  ).toString("base64url");
  const sig = sign("RSA-SHA256", Buffer.from(`${h}.${p}`), rotated.privateKey);
  return `${h}.${p}.${sig.toString("base64url")}`;
})();
const [s3, v3] = (await handler(event("/")).then((r) => header(r, "set-cookie")[0]))
  .split(";")[0]
  .split("=")[1]
  .split(".");
const afterRotation = await handler(
  event("/_auth/callback", {
    qs: `code=abc&state=${s3}`,
    cookie: `n8n_edge_pkce=${s3}.${v3}.`,
  }),
);
assert.equal(afterRotation.status, "302");
assert.ok(calls.filter((c) => c.url.endsWith("jwks.json")).length > jwks);

// 9. Sign-out clears the session and goes to Cognito's logout.
const out = await handler(event("/_auth/signout"));
assert.equal(out.status, "302");
assert.equal(new URL(header(out, "location")[0]).pathname, "/logout");
assert.match(header(out, "set-cookie")[0], /^n8n_edge_token=; .*Max-Age=0$/);

console.log("edge auth: all checks passed");
