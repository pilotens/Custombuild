export type AppEnvironment = "development" | "test" | "production";

export interface PublicRuntimeConfig {
  appEnv: AppEnvironment;
  apiUrl?: string;
  /** Public, non-secret development credential. Forbidden in production. */
  developmentToken?: string;
  oidcIssuer?: string;
  oidcClientId?: string;
  oidcRedirectUri?: string;
}

export type RuntimeEnvironment = Readonly<Record<string, string | undefined>>;

export const RUNTIME_ENVIRONMENT_KEYS = {
  apiUrl: "CUSTOMBUILD_WEB_API_URL",
  developmentToken: "CUSTOMBUILD_WEB_DEMO_TOKEN",
  oidcIssuer: "CUSTOMBUILD_WEB_OIDC_ISSUER",
  oidcClientId: "CUSTOMBUILD_WEB_OIDC_CLIENT_ID",
  oidcRedirectUri: "CUSTOMBUILD_WEB_OIDC_REDIRECT_URI",
} as const;

const APP_ENVIRONMENTS = new Set<AppEnvironment>(["development", "test", "production"]);
const CONTROL_CHARACTER = /[\u0000-\u001f\u007f]/;

export class PublicRuntimeConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PublicRuntimeConfigError";
  }
}

function optionalValue(environment: RuntimeEnvironment, key: string): string | undefined {
  const value = environment[key];
  return value === undefined || value === "" ? undefined : value;
}

function appEnvironment(environment: RuntimeEnvironment): AppEnvironment {
  if (environment.APP_ENV === "") {
    throw new PublicRuntimeConfigError("APP_ENV får inte vara tomt.");
  }
  const value = environment.APP_ENV
    ?? (environment.NODE_ENV === "production" ? "production" : "development");
  if (!APP_ENVIRONMENTS.has(value as AppEnvironment)) {
    throw new PublicRuntimeConfigError("APP_ENV måste vara development, test eller production.");
  }
  return value as AppEnvironment;
}

function parsedUrl(
  value: string,
  label: string,
  {
    httpsOnly,
    exactOrigin = false,
    rootPath = false,
  }: { httpsOnly: boolean; exactOrigin?: boolean; rootPath?: boolean },
): URL {
  if (value !== value.trim()) {
    throw new PublicRuntimeConfigError(`${label} får inte innehålla omgivande blanksteg.`);
  }
  if (CONTROL_CHARACTER.test(value) || value.includes("\\")) {
    throw new PublicRuntimeConfigError(`${label} använder inte en tillåten säker URL.`);
  }
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new PublicRuntimeConfigError(`${label} måste vara en giltig URL.`);
  }
  const authorityContainsCredentials = /^https?:\/\/[^/?#]*@/i.test(value);
  const protocolAllowed = httpsOnly
    ? parsed.protocol === "https:"
    : parsed.protocol === "http:" || parsed.protocol === "https:";
  if (
    !protocolAllowed
    || parsed.username
    || parsed.password
    || authorityContainsCredentials
    || value.includes("?")
    || value.includes("#")
  ) {
    throw new PublicRuntimeConfigError(`${label} använder inte en tillåten säker URL.`);
  }
  if (exactOrigin && parsed.pathname !== "/") {
    throw new PublicRuntimeConfigError(`${label} måste vara ett exakt origin utan sökväg.`);
  }
  if (rootPath && parsed.pathname !== "/") {
    throw new PublicRuntimeConfigError(`${label} måste vara webbappens exakta rot.`);
  }
  return parsed;
}

function clientId(value: string): string {
  if (
    value !== value.trim()
    || value.length > 200
    || CONTROL_CHARACTER.test(value)
  ) {
    throw new PublicRuntimeConfigError("OIDC client-id är ogiltigt.");
  }
  return value;
}

function developmentToken(value: string | undefined): string | undefined {
  if (value === undefined) return undefined;
  if (
    value !== value.trim()
    || value.length > 2_048
    || CONTROL_CHARACTER.test(value)
  ) {
    throw new PublicRuntimeConfigError("Den offentliga utvecklingstoken är ogiltig.");
  }
  return value;
}

/**
 * Select and validate the exact non-secret configuration allowed to cross the
 * server/client boundary. Unrelated process environment values are never copied.
 */
export function parsePublicRuntimeConfig(
  environment: RuntimeEnvironment,
): PublicRuntimeConfig {
  const appEnv = appEnvironment(environment);
  const rawApiUrl = optionalValue(environment, RUNTIME_ENVIRONMENT_KEYS.apiUrl);
  const rawDevelopmentToken = optionalValue(
    environment,
    RUNTIME_ENVIRONMENT_KEYS.developmentToken,
  );
  const rawIssuer = optionalValue(environment, RUNTIME_ENVIRONMENT_KEYS.oidcIssuer);
  const rawClientId = optionalValue(environment, RUNTIME_ENVIRONMENT_KEYS.oidcClientId);
  const rawRedirectUri = optionalValue(environment, RUNTIME_ENVIRONMENT_KEYS.oidcRedirectUri);

  if (appEnv === "production" && rawDevelopmentToken !== undefined) {
    throw new PublicRuntimeConfigError(
      "CUSTOMBUILD_WEB_DEMO_TOKEN måste vara tom i production.",
    );
  }
  if (appEnv === "production" && rawApiUrl === undefined) {
    throw new PublicRuntimeConfigError(
      "CUSTOMBUILD_WEB_API_URL krävs i production.",
    );
  }

  const oidcCount = [rawIssuer, rawClientId, rawRedirectUri]
    .filter((value) => value !== undefined).length;
  if (oidcCount !== 0 && oidcCount !== 3) {
    throw new PublicRuntimeConfigError(
      "OIDC-konfigurationen måste ange issuer, client-id och redirect-URI tillsammans.",
    );
  }
  if (appEnv === "production" && oidcCount !== 3) {
    throw new PublicRuntimeConfigError("Fullständig OIDC-konfiguration krävs i production.");
  }

  const apiUrl = rawApiUrl === undefined
    ? undefined
    : parsedUrl(rawApiUrl, "Webb-API-adressen", {
      httpsOnly: appEnv === "production",
      exactOrigin: true,
    }).origin;
  const oidcIssuer = rawIssuer === undefined
    ? undefined
    : parsedUrl(rawIssuer, "OIDC-utfärdaren", { httpsOnly: true })
      .href.replace(/\/$/, "");
  const oidcRedirectUri = rawRedirectUri === undefined
    ? undefined
    : parsedUrl(rawRedirectUri, "OIDC callback-adressen", {
      httpsOnly: true,
      rootPath: true,
    }).href;

  return Object.freeze({
    appEnv,
    ...(apiUrl ? { apiUrl } : {}),
    ...(rawDevelopmentToken
      ? { developmentToken: developmentToken(rawDevelopmentToken) }
      : {}),
    ...(oidcIssuer ? { oidcIssuer } : {}),
    ...(rawClientId ? { oidcClientId: clientId(rawClientId) } : {}),
    ...(oidcRedirectUri ? { oidcRedirectUri } : {}),
  });
}

export function loadPublicRuntimeConfig(
  environment: RuntimeEnvironment = process.env,
): PublicRuntimeConfig {
  return parsePublicRuntimeConfig(environment);
}
