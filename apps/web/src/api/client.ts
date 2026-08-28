const API_PREFIX = "/api/v1/";

interface ProblemDetails {
  readonly code?: unknown;
  readonly detail?: unknown;
}

export class ApiRequestError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.code = code;
  }
}

function isSameOriginApiPath(path: string): boolean {
  return path.startsWith(API_PREFIX) && !path.startsWith("//");
}

async function safeProblem(response: Response): Promise<ProblemDetails> {
  try {
    const value: unknown = await response.json();
    if (typeof value === "object" && value !== null && !Array.isArray(value)) {
      return value;
    }
  } catch {
    // The response boundary below deliberately uses a stable local fallback.
  }
  return {};
}

export async function getJson(
  path: string,
  signal?: AbortSignal,
): Promise<unknown> {
  if (!isSameOriginApiPath(path)) {
    throw new ApiRequestError(
      0,
      "unsafe_api_path",
      "The API request path is not allowed.",
    );
  }

  let response: Response;
  try {
    const request: RequestInit = {
      method: "GET",
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    };
    if (signal !== undefined) {
      request.signal = signal;
    }
    response = await fetch(path, request);
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    throw new ApiRequestError(
      0,
      "api_unreachable",
      "The local API is not ready. Start it and try again.",
    );
  }

  if (!response.ok) {
    const problem = await safeProblem(response);
    const code =
      typeof problem.code === "string" ? problem.code : "api_request_failed";
    const detail =
      typeof problem.detail === "string"
        ? problem.detail
        : "The local API could not complete this request.";
    throw new ApiRequestError(response.status, code, detail);
  }

  try {
    return (await response.json()) as unknown;
  } catch {
    throw new ApiRequestError(
      response.status,
      "invalid_json_response",
      "The local API returned an invalid response.",
    );
  }
}
