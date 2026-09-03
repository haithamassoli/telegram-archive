// Read side for the batch commands: the §4.2 skip fast-path, the §4.6 failure
// drain, and the M1 validation pass.
import { query } from "./_generated/server";
import { v } from "convex/values";

export const channelByUsername = query({
  args: { username: v.string() },
  handler: async (ctx, args) => {
    const rows = await ctx.db
      .query("channels")
      .withIndex("by_username", (q) => q.eq("username", args.username))
      .collect();
    return rows[0] ?? null;
  },
});

// §4.2 skip rule for ingest: a message counts as done when its row exists and,
// when it carries a binary, that binary is already linked. A crash between the
// message row and its media leaves the message incomplete, and this reports it
// as such — which is what keeps a resumed run from re-downloading a whole batch.
export const ingestedMessageIds = query({
  args: {
    channelId: v.id("channels"),
    fromId: v.number(),
    toId: v.number(),
  },
  handler: async (ctx, args) => {
    const rows = await ctx.db
      .query("telegramMessages")
      .withIndex("by_channel_message", (q) =>
        q
          .eq("channelId", args.channelId)
          .gte("telegramMessageId", args.fromId)
          .lte("telegramMessageId", args.toId),
      )
      .collect();
    const done: number[] = [];
    for (const row of rows) {
      if (row.mediaType === "none") {
        done.push(row.telegramMessageId);
        continue;
      }
      const media = await ctx.db
        .query("messageMedia")
        .withIndex("by_message", (q) => q.eq("messageId", row._id))
        .first();
      if (media !== null) done.push(row.telegramMessageId);
    }
    return done;
  },
});

// Asked after hashing a download and before uploading: a binary that already has
// a row keeps that row's r2Key, so the same bytes can never land under two keys
// because a repost arrived with a different filename extension.
export const mediaObjectBySha256 = query({
  args: { sha256: v.string() },
  handler: async (ctx, args) => {
    const rows = await ctx.db
      .query("mediaObjects")
      .withIndex("by_sha256", (q) => q.eq("sha256", args.sha256))
      .collect();
    if (rows.length > 1) {
      throw new Error(
        `integrity error: ${rows.length} rows for unique mediaObjects.sha256=${args.sha256}`,
      );
    }
    return rows[0] ?? null;
  },
});

export const unresolvedFailures = query({
  args: { stage: v.string() },
  handler: async (ctx, args) =>
    await ctx.db
      .query("failures")
      .withIndex("by_stage_resolved", (q) =>
        q.eq("stage", args.stage).eq("resolved", false),
      )
      .collect(),
});

// Validation pass (M1 exit): counts, and a page of messages to spot-check
// telegramUrl against live Telegram. Paged by message id — a channel has tens of
// thousands of rows and a single `.collect()` would hit the query read limit.
export const channelScan = query({
  args: {
    channelId: v.id("channels"),
    fromId: v.number(),
    limit: v.number(),
  },
  handler: async (ctx, args) => {
    const rows = await ctx.db
      .query("telegramMessages")
      .withIndex("by_channel_message", (q) =>
        q.eq("channelId", args.channelId).gte("telegramMessageId", args.fromId),
      )
      .take(args.limit);
    let withMedia = 0;
    // A message claiming a binary with no `messageMedia` row is the one failure
    // M1 must never report as archived: the blob is missing, not merely absent.
    const unlinked: number[] = [];
    for (const row of rows) {
      if (row.mediaType === "none") continue;
      withMedia += 1;
      const link = await ctx.db
        .query("messageMedia")
        .withIndex("by_message", (q) => q.eq("messageId", row._id))
        .first();
      if (link === null) unlinked.push(row.telegramMessageId);
    }
    return {
      count: rows.length,
      withMedia,
      unlinked,
      ids: rows.map((row) => row.telegramMessageId),
      nextFromId: rows.length
        ? rows[rows.length - 1].telegramMessageId + 1
        : null,
    };
  },
});

export const messagesByIds = query({
  args: { channelId: v.id("channels"), ids: v.array(v.number()) },
  handler: async (ctx, args) => {
    const out = [];
    for (const id of args.ids) {
      const row = await ctx.db
        .query("telegramMessages")
        .withIndex("by_channel_message", (q) =>
          q.eq("channelId", args.channelId).eq("telegramMessageId", id),
        )
        .first();
      if (row !== null) out.push(row);
    }
    return out;
  },
});

export const mediaForMessage = query({
  args: { messageId: v.id("telegramMessages") },
  handler: async (ctx, args) => {
    const links = await ctx.db
      .query("messageMedia")
      .withIndex("by_message", (q) => q.eq("messageId", args.messageId))
      .collect();
    const out = [];
    for (const link of links) {
      const object = await ctx.db.get(link.mediaObjectId);
      if (object !== null) out.push(object);
    }
    return out;
  },
});
