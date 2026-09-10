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

// M2 selection (§4.2/§4.3): one page of unique binaries in sha256 order, each
// carrying its part-transcript row for the active configHash. sha256 order makes
// the scan resumable, and joining the transcript here is what lets a worker take
// the fast path without a second round trip per object.
export const mediaObjectsPage = query({
  args: { configHash: v.string(), cursor: v.string(), limit: v.number() },
  handler: async (ctx, args) => {
    const rows = await ctx.db
      .query("mediaObjects")
      .withIndex("by_sha256", (q) => q.gt("sha256", args.cursor))
      .take(args.limit);
    const objects = [];
    for (const row of rows) {
      const found = await ctx.db
        .query("partTranscripts")
        .withIndex("by_sha256_config", (q) =>
          q.eq("sha256", row.sha256).eq("configHash", args.configHash),
        )
        .collect();
      if (found.length > 1) {
        throw new Error(
          `integrity error: ${found.length} rows for unique partTranscripts.(${row.sha256},${args.configHash})`,
        );
      }
      const transcript = found[0] ?? null;
      objects.push({
        sha256: row.sha256,
        r2Key: row.r2Key,
        ext: row.ext,
        mimeType: row.mimeType ?? null,
        durationMs: row.durationMs ?? null,
        sizeBytes: row.sizeBytes,
        status: transcript?.status ?? null,
        processingStartedAt: transcript?.processingStartedAt ?? null,
        attempts: transcript?.attempts ?? 0,
        rawR2Key: transcript?.rawR2Key ?? null,
      });
    }
    return {
      objects,
      nextCursor: rows.length ? rows[rows.length - 1].sha256 : null,
    };
  },
});

// M3 selection: one page of a channel's messages in telegramMessageId order,
// each carrying the binaries it links. Ordering by message id is what makes the
// Organizer's "title then the audio that follows it" rule expressible at all,
// and joining the media here saves a round trip per message on a 26k-row scan.
export const messagesPage = query({
  args: {
    channelId: v.id("channels"),
    cursor: v.number(),
    limit: v.number(),
  },
  handler: async (ctx, args) => {
    const rows = await ctx.db
      .query("telegramMessages")
      .withIndex("by_channel_message", (q) =>
        q.eq("channelId", args.channelId).gt("telegramMessageId", args.cursor),
      )
      .take(args.limit);
    const messages = [];
    for (const row of rows) {
      const links = await ctx.db
        .query("messageMedia")
        .withIndex("by_message", (q) => q.eq("messageId", row._id))
        .collect();
      const media = [];
      for (const link of links) {
        const object = await ctx.db.get(link.mediaObjectId);
        if (object === null) {
          throw new Error(
            `integrity error: messageMedia ${link._id} points at a missing mediaObject`,
          );
        }
        media.push({
          mediaObjectId: object._id,
          originalFileName: link.originalFileName ?? null,
          sha256: object.sha256,
          ext: object.ext,
          mimeType: object.mimeType ?? null,
          durationMs: object.durationMs ?? null,
        });
      }
      messages.push({
        id: row._id,
        telegramMessageId: row.telegramMessageId,
        date: row.date,
        editDate: row.editDate ?? null,
        deletedAt: row.deletedAt ?? null,
        text: row.text ?? null,
        replyToMessageId: row.replyToMessageId ?? null,
        groupedId: row.groupedId ?? null,
        telegramUrl: row.telegramUrl,
        mediaType: row.mediaType,
        semanticType: row.semanticType,
        classifierVersion: row.classifierVersion ?? null,
        isForwarded: row.isForwarded,
        forwardedFromChannel: row.forwardedFromChannel ?? null,
        media,
      });
    }
    return {
      messages,
      nextCursor: rows.length
        ? rows[rows.length - 1].telegramMessageId
        : null,
    };
  },
});

// Phase 3.5 selection: lessons with their ordered parts. `lessonTranscriptR2Key`
// rides along so the builder can take the §4.2 fast path — a pointer already
// naming the current assemblyHash + configHash means there is nothing to build.
export const lessonsPage = query({
  args: { cursor: v.string(), limit: v.number() },
  handler: async (ctx, args) => {
    const rows = await ctx.db
      .query("lessons")
      .withIndex("by_lesson_key", (q) => q.gt("lessonKey", args.cursor))
      .take(args.limit);
    const lessons = [];
    for (const row of rows) {
      const parts = await ctx.db
        .query("lessonParts")
        .withIndex("by_lesson_order", (q) => q.eq("lessonId", row._id))
        .collect();
      parts.sort((a, b) => a.order - b.order);
      const withSha = [];
      for (const part of parts) {
        const object = await ctx.db.get(part.mediaObjectId);
        if (object === null) {
          throw new Error(
            `integrity error: lessonPart ${part._id} points at a missing mediaObject`,
          );
        }
        withSha.push({
          sha256: object.sha256,
          order: part.order,
          offsetMs: part.offsetMs,
          durationMs: part.durationMs,
        });
      }
      lessons.push({
        id: row._id,
        lessonKey: row.lessonKey,
        assemblyHash: row.assemblyHash,
        rawTitle: row.rawTitle,
        normalizedTitle: row.normalizedTitle,
        seriesName: row.seriesName ?? null,
        seriesEpisode: row.seriesEpisode ?? null,
        reviewStatus: row.reviewStatus,
        durationMs: row.durationMs,
        lessonTranscriptR2Key: row.lessonTranscriptR2Key ?? null,
        parts: withSha,
      });
    }
    return {
      lessons,
      nextCursor: rows.length ? rows[rows.length - 1].lessonKey : null,
    };
  },
});
