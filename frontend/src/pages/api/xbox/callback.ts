import type { APIRoute } from "astro";

const BACKEND_PORT = (import.meta.env.BACKEND_PORT as string | undefined) ?? "7331";
const BACKEND = `http://localhost:${BACKEND_PORT}`;

export const GET: APIRoute = async ({ request }) => {
  const incoming = new URL(request.url);
  const backendUrl = new URL("/xbox/callback", BACKEND);
  backendUrl.search = incoming.search;

  try {
    const response = await fetch(backendUrl, { redirect: "manual" });

    if (response.status >= 300 && response.status < 400) {
      return Response.redirect(new URL("/connections?xbox=connected", request.url), 303);
    }

    const detail = await response.text();
    return new Response(
      `Xbox authentication failed (HTTP ${response.status}). ${detail}`,
      {
        status: response.status,
        headers: { "Content-Type": "text/plain; charset=utf-8" },
      },
    );
  } catch (error) {
    return new Response(
      `Xbox authentication callback failed: ${error instanceof Error ? error.message : String(error)}`,
      { status: 502, headers: { "Content-Type": "text/plain; charset=utf-8" } },
    );
  }
};
