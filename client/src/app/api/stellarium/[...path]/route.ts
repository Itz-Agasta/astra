import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

function stellariumOrigin(): string {
  const host = process.env.CCE_STELLARIUM_HOST ?? "127.0.0.1";
  const port = process.env.CCE_STELLARIUM_PORT ?? "8090";
  return `http://${host}:${port}`;
}

async function proxy(req: Request, path: string[]): Promise<Response> {
  const origin = stellariumOrigin();
  const joined = path.join("/");
  const incoming = new URL(req.url);
  const target = `${origin}/api/${joined}${incoming.search}`;

  try {
    const headers = new Headers();
    const contentType = req.headers.get("content-type");
    if (contentType) {
      headers.set("content-type", contentType);
    }

    const res = await fetch(target, {
      method: req.method,
      headers,
      body: req.method === "GET" || req.method === "HEAD" ? undefined : await req.arrayBuffer(),
      cache: "no-store",
      signal: AbortSignal.timeout(4000),
    });

    const out = new Headers();
    const pass = res.headers.get("content-type");
    if (pass) {
      out.set("content-type", pass);
    }
    out.set("cache-control", "no-store");

    return new Response(res.body, { status: res.status, headers: out });
  } catch {
    return NextResponse.json(
      { error: "stellarium unreachable", origin },
      { status: 502 },
    );
  }
}

type RouteCtx = { params: Promise<{ path: string[] }> };

export async function GET(req: Request, ctx: RouteCtx) {
  const { path } = await ctx.params;
  return proxy(req, path);
}

export async function POST(req: Request, ctx: RouteCtx) {
  const { path } = await ctx.params;
  return proxy(req, path);
}
