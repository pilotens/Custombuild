const ACCESS_TOKEN_KEY = "custombuild:oidc:access-token";
const PKCE_STATE_KEY = "custombuild:oidc:state";
const PKCE_VERIFIER_KEY = "custombuild:oidc:verifier";

interface StoredToken {
  accessToken: string;
  expiresAt: number;
}

export interface OidcDiscovery {
  issuer: string;
  authorization_endpoint: string;
  token_endpoint: string;
}

function configuredIssuer(): string | undefined {
  return process.env.NEXT_PUBLIC_OIDC_ISSUER?.replace(/\/$/, "");
}

function configuredClientId(): string | undefined {
  return process.env.NEXT_PUBLIC_OIDC_CLIENT_ID;
}

function redirectUri(): string {
  if (typeof window === "undefined") return "";
  const expected = new URL("/", window.location.origin);
  const configured = new URL(process.env.NEXT_PUBLIC_OIDC_REDIRECT_URI || expected.href);
  if (
    configured.protocol !== "https:"
    || configured.origin !== expected.origin
    || configured.pathname !== "/"
    || configured.search
    || configured.hash
    || configured.username
    || configured.password
  ) {
    throw new Error("OIDC callback-adressen måste vara webbappens exakta HTTPS-rot.");
  }
  return configured.href;
}

function randomBase64Url(bytes = 32): string {
  const value = crypto.getRandomValues(new Uint8Array(bytes));
  return btoa(String.fromCharCode(...value))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

async function sha256Base64Url(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return btoa(String.fromCharCode(...new Uint8Array(digest)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

function assertHttpsEndpoint(value: string, label: string): URL {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`${label} måste vara en giltig HTTPS-adress.`);
  }
  if (url.protocol !== "https:" || url.username || url.password || url.hash) {
    throw new Error(`${label} måste vara en säker HTTPS-adress.`);
  }
  return url;
}

export function validateOidcDiscovery(
  payload: Partial<OidcDiscovery>,
  configuredIssuerValue: string,
): OidcDiscovery {
  const issuerUrl = assertHttpsEndpoint(configuredIssuerValue, "OIDC-utfärdaren");
  if (issuerUrl.search) {
    throw new Error("OIDC-utfärdaren får inte innehålla en query.");
  }
  const expectedIssuer = issuerUrl.href.replace(/\/$/, "");
  let discoveredIssuer: URL;
  try {
    discoveredIssuer = assertHttpsEndpoint(payload.issuer ?? "", "OIDC-konfigurationens issuer");
  } catch {
    throw new Error("OIDC-konfigurationens issuer matchar inte den konfigurerade utfärdaren.");
  }
  if (discoveredIssuer.href.replace(/\/$/, "") !== expectedIssuer) {
    throw new Error("OIDC-konfigurationens issuer matchar inte den konfigurerade utfärdaren.");
  }
  if (!payload.authorization_endpoint || !payload.token_endpoint) {
    throw new Error("OIDC-konfigurationen saknar authorization- eller token-endpoint.");
  }
  const authorization = assertHttpsEndpoint(
    payload.authorization_endpoint,
    "OIDC authorization-endpoint",
  );
  const token = assertHttpsEndpoint(payload.token_endpoint, "OIDC token-endpoint");
  if (authorization.origin !== issuerUrl.origin || token.origin !== issuerUrl.origin) {
    throw new Error("OIDC-endpoints måste använda samma betrodda origin som utfärdaren.");
  }
  return {
    issuer: discoveredIssuer.href.replace(/\/$/, ""),
    authorization_endpoint: authorization.href,
    token_endpoint: token.href,
  };
}

async function discovery(): Promise<OidcDiscovery> {
  const issuer = configuredIssuer();
  if (!issuer) throw new Error("OIDC-utfärdare är inte konfigurerad.");
  const issuerUrl = assertHttpsEndpoint(issuer, "OIDC-utfärdaren");
  if (issuerUrl.search || issuerUrl.hash) {
    throw new Error("OIDC-utfärdaren får inte innehålla query eller fragment.");
  }
  const response = await fetch(`${issuer}/.well-known/openid-configuration`, {
    headers: { Accept: "application/json" },
    credentials: "omit",
    redirect: "error",
  });
  if (!response.ok) throw new Error("Kunde inte läsa OIDC-konfigurationen.");
  const payload = await response.json() as Partial<OidcDiscovery>;
  return validateOidcDiscovery(payload, issuer);
}

export function oidcConfigured(): boolean {
  return Boolean(configuredIssuer() && configuredClientId());
}

export function getStoredAccessToken(): string | undefined {
  if (typeof window === "undefined") return undefined;
  try {
    const raw = window.sessionStorage.getItem(ACCESS_TOKEN_KEY);
    if (!raw) return undefined;
    const parsed = JSON.parse(raw) as Partial<StoredToken>;
    if (!parsed.accessToken || !parsed.expiresAt || parsed.expiresAt <= Date.now() + 15_000) {
      window.sessionStorage.removeItem(ACCESS_TOKEN_KEY);
      return undefined;
    }
    return parsed.accessToken;
  } catch {
    window.sessionStorage.removeItem(ACCESS_TOKEN_KEY);
    return undefined;
  }
}

export async function beginOidcLogin(): Promise<void> {
  const clientId = configuredClientId();
  if (!clientId) throw new Error("OIDC client-id är inte konfigurerat.");
  const config = await discovery();
  const state = randomBase64Url();
  const verifier = randomBase64Url(48);
  window.sessionStorage.setItem(PKCE_STATE_KEY, state);
  window.sessionStorage.setItem(PKCE_VERIFIER_KEY, verifier);
  const authorization = new URL(config.authorization_endpoint);
  authorization.searchParams.set("client_id", clientId);
  authorization.searchParams.set("redirect_uri", redirectUri());
  authorization.searchParams.set("response_type", "code");
  authorization.searchParams.set("scope", "openid profile email");
  authorization.searchParams.set("state", state);
  authorization.searchParams.set("code_challenge", await sha256Base64Url(verifier));
  authorization.searchParams.set("code_challenge_method", "S256");
  window.location.assign(authorization);
}

export async function completeOidcCallback(): Promise<boolean> {
  if (typeof window === "undefined") return false;
  const current = new URL(window.location.href);
  const code = current.searchParams.get("code");
  const returnedState = current.searchParams.get("state");
  if (!code && !returnedState) return Boolean(getStoredAccessToken());
  const expectedState = window.sessionStorage.getItem(PKCE_STATE_KEY);
  const verifier = window.sessionStorage.getItem(PKCE_VERIFIER_KEY);
  if (!code || !returnedState || !expectedState || returnedState !== expectedState || !verifier) {
    throw new Error("OIDC-inloggningen kunde inte verifieras. Försök igen.");
  }
  const clientId = configuredClientId();
  if (!clientId) throw new Error("OIDC client-id är inte konfigurerat.");
  const config = await discovery();
  const response = await fetch(config.token_endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
    credentials: "omit",
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: clientId,
      redirect_uri: redirectUri(),
      code,
      code_verifier: verifier,
    }),
  });
  if (!response.ok) throw new Error("OIDC-koden kunde inte växlas mot en åtkomsttoken.");
  const token = await response.json() as { access_token?: unknown; expires_in?: unknown };
  if (typeof token.access_token !== "string" || token.access_token.length < 20) {
    throw new Error("OIDC-svaret saknar en giltig åtkomsttoken.");
  }
  const expiresIn = typeof token.expires_in === "number" ? token.expires_in : 300;
  window.sessionStorage.setItem(ACCESS_TOKEN_KEY, JSON.stringify({
    accessToken: token.access_token,
    expiresAt: Date.now() + Math.max(60, expiresIn) * 1_000,
  } satisfies StoredToken));
  window.sessionStorage.removeItem(PKCE_STATE_KEY);
  window.sessionStorage.removeItem(PKCE_VERIFIER_KEY);
  current.searchParams.delete("code");
  current.searchParams.delete("state");
  current.searchParams.delete("session_state");
  window.history.replaceState({}, "", `${current.pathname}${current.search}${current.hash}`);
  return true;
}

export function clearOidcSession(): void {
  if (typeof window === "undefined") return;
  window.sessionStorage.removeItem(ACCESS_TOKEN_KEY);
  window.sessionStorage.removeItem(PKCE_STATE_KEY);
  window.sessionStorage.removeItem(PKCE_VERIFIER_KEY);
}
