import { getBookmarks, counts } from "./db";
import { page } from "./render";

const HOST = Bun.env.HOST ?? "0.0.0.0";
const PORT = Number(Bun.env.PORT ?? "9005");

const server = Bun.serve({
  hostname: HOST,
  port: PORT,
  async fetch(request: Request) {
    const { pathname } = new URL(request.url);

    if (pathname === "/api/health") {
      return Response.json({ ok: true });
    }

    if (pathname === "/api/bookmarks") {
      const data = await getBookmarks();
      return Response.json(data, {
        headers: { "Cache-Control": "no-store" },
      });
    }

    if (pathname === "/" || pathname === "") {
      try {
        const [bookmarks, c] = await Promise.all([getBookmarks(), counts()]);
        return new Response(page(bookmarks, c), {
          headers: {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
          },
        });
      } catch (err) {
        console.error("[x-bookmarks] render failed:", err);
        return new Response("Internal error: " + (err as Error).message, {
          status: 500,
        });
      }
    }

    return new Response("Not found", { status: 404 });
  },
});

console.log(`[x-bookmarks] listening on http://${HOST}:${server.port}`);
