import postgres from "postgres";

const DATABASE_URL = Bun.env.DATABASE_URL;
if (!DATABASE_URL) {
  throw new Error("DATABASE_URL environment variable is required");
}

const sql = postgres(DATABASE_URL, { max: 4 });

export interface User {
  id: string;
  name: string | null;
  username: string | null;
  profile_image_url: string | null;
}

export interface Media {
  media_key: string;
  type: string | null;
  url: string | null;
  preview_image_url: string | null;
}

export interface PublicMetrics {
  reply_count?: number;
  retweet_count?: number;
  like_count?: number;
  quote_count?: number;
  bookmark_count?: number;
  impression_count?: number;
}

export interface Tweet {
  id: string;
  author: User | null;
  conversation_id: string | null;
  text: string;
  kind: string;
  referenced_tweet_id: string | null;
  referenced_kind: string | null;
  created_at: string | null;
  metrics: PublicMetrics;
  media: Media[];
}

export interface Bookmark {
  tweet: Tweet;
  bookmarked_at: string;
  sort_index: number | null;
  quoted: Tweet | null;
  thread: Tweet[];
}

type RawTweet = {
  id: string;
  author_id: string | null;
  conversation_id: string | null;
  text: string | null;
  kind: string;
  referenced_tweet_id: string | null;
  referenced_kind: string | null;
  media_keys: unknown;
  payload: unknown;
};

function asObj(v: unknown): Record<string, any> {
  if (v && typeof v === "object") return v as Record<string, any>;
  if (typeof v === "string") {
    try {
      return JSON.parse(v);
    } catch {
      return {};
    }
  }
  return {};
}

function asKeys(v: unknown): string[] {
  if (Array.isArray(v)) return v as string[];
  if (typeof v === "string") {
    try {
      const p = JSON.parse(v);
      return Array.isArray(p) ? p : [];
    } catch {
      return [];
    }
  }
  return [];
}

/** Load everything and assemble bookmarks → tweet + author + media + quote + thread. */
export async function getBookmarks(): Promise<Bookmark[]> {
  const [users, media, tweets, bookmarkRows] = await Promise.all([
    sql<User[]>`SELECT id, name, username, profile_image_url FROM x_users`,
    sql<Media[]>`SELECT media_key, type, url, preview_image_url FROM x_media`,
    sql<RawTweet[]>`
      SELECT id, author_id, conversation_id, text, kind,
             referenced_tweet_id, referenced_kind, media_keys, payload
        FROM x_tweets`,
    sql<{ tweet_id: string; bookmarked_at: string; sort_index: number | null }[]>`
      SELECT tweet_id, bookmarked_at, sort_index FROM x_bookmarks`,
  ]);

  const userById = new Map(users.map((u) => [u.id, u]));
  const mediaByKey = new Map(media.map((m) => [m.media_key, m]));

  const tweetById = new Map<string, Tweet>();
  // conversation_id → thread_reply tweets, for thread expansion
  const repliesByConv = new Map<string, Tweet[]>();

  for (const r of tweets) {
    const payload = asObj(r.payload);
    const t: Tweet = {
      id: r.id,
      author: r.author_id ? userById.get(r.author_id) ?? null : null,
      conversation_id: r.conversation_id,
      text: r.text ?? "",
      kind: r.kind,
      referenced_tweet_id: r.referenced_tweet_id,
      referenced_kind: r.referenced_kind,
      created_at: payload.created_at ?? null,
      metrics: (payload.public_metrics as PublicMetrics) ?? {},
      media: asKeys(r.media_keys)
        .map((k) => mediaByKey.get(k))
        .filter((m): m is Media => Boolean(m)),
    };
    tweetById.set(t.id, t);
    if (t.kind === "thread_reply" && t.conversation_id) {
      const arr = repliesByConv.get(t.conversation_id) ?? [];
      arr.push(t);
      repliesByConv.set(t.conversation_id, arr);
    }
  }

  const byCreated = (a: Tweet, b: Tweet) =>
    (a.created_at ?? "").localeCompare(b.created_at ?? "");

  const out: Bookmark[] = [];
  for (const b of bookmarkRows) {
    const tweet = tweetById.get(b.tweet_id);
    if (!tweet) continue;

    let quoted: Tweet | null = null;
    if (tweet.referenced_kind === "quoted" && tweet.referenced_tweet_id) {
      quoted = tweetById.get(tweet.referenced_tweet_id) ?? null;
    }

    const thread = tweet.conversation_id
      ? (repliesByConv.get(tweet.conversation_id) ?? [])
          .filter((r) => r.id !== tweet.id)
          .sort(byCreated)
      : [];

    out.push({
      tweet,
      bookmarked_at: b.bookmarked_at,
      sort_index: b.sort_index,
      quoted,
      thread,
    });
  }

  // Order: bookmarks with a real (live-fetched) sort_index first, in index
  // order; everything else falls back to tweet date, newest first.
  out.sort((a, b) => {
    const ai = a.sort_index;
    const bi = b.sort_index;
    if (ai != null && bi != null) return ai - bi;
    if (ai != null) return -1;
    if (bi != null) return 1;
    return (b.tweet.created_at ?? "").localeCompare(a.tweet.created_at ?? "");
  });
  return out;
}

export async function counts(): Promise<Record<string, number>> {
  const [[b], [t], [u], [m]] = await Promise.all([
    sql`SELECT COUNT(*)::int AS n FROM x_bookmarks`,
    sql`SELECT COUNT(*)::int AS n FROM x_tweets`,
    sql`SELECT COUNT(*)::int AS n FROM x_users`,
    sql`SELECT COUNT(*)::int AS n FROM x_media`,
  ]);
  return { bookmarks: b.n, tweets: t.n, users: u.n, media: m.n };
}
