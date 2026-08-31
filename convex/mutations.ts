// The four atomic uniqueness mutations (plan §2 / §0 item 3).
//
// Convex indexes are not SQL UNIQUE constraints. Mutations are serializable, so
// lookup-then-write inside one mutation is what enforces the invariant. Finding
// more than one row on a "unique" query is an integrity error and always throws
// — it is never resolved by silently picking one.
import { mutation } from "./_generated/server";
import { v } from "convex/values";

const STALE_HEARTBEAT_MS = 5 * 60 * 1000; // §4.5

function exactlyZeroOrOne<T>(rows: T[], what: string): T | null {
  if (rows.length > 1) {
    throw new Error(
      `integrity error: ${rows.length} rows for unique ${what} — expected at most 1`,
    );
  }
  return rows[0] ?? null;
}

export const getOrCreateMediaObject = mutation({
  args: {
    sha256: v.string(),
    r2Key: v.string(),
    ext: v.string(),
    sizeBytes: v.number(),
    mimeType: v.optional(v.string()),
    durationMs: v.optional(v.number()),
    codec: v.optional(v.string()),
    sampleRate: v.optional(v.number()),
    channelCount: v.optional(v.number()),
  },
  handler: async (ctx, args) => {
    const rows = await ctx.db
      .query("mediaObjects")
      .withIndex("by_sha256", (q) => q.eq("sha256", args.sha256))
      .collect();
    const existing = exactlyZeroOrOne(rows, `mediaObjects.sha256=${args.sha256}`);
    if (existing !== null) {
      return { id: existing._id, created: false };
    }
    const id = await ctx.db.insert("mediaObjects", {
      ...args,
      firstSeenAt: Date.now(),
    });
    return { id, created: true };
  },
});

export const upsertPartTranscript = mutation({
  args: {
    sha256: v.string(),
    configHash: v.string(),
    status: v.union(
      v.literal("pending"),
      v.literal("processing"),
      v.literal("done"),
      v.literal("failed"),
    ),
    processingRunId: v.optional(v.string()),
    model: v.optional(v.string()),
    modelRevision: v.optional(v.string()),
    rawR2Key: v.optional(v.string()),
    durationMs: v.optional(v.number()),
    segmentCount: v.optional(v.number()),
    error: v.optional(v.string()),
  },
  handler: async (ctx, args) => {
    const { sha256, configHash, status, ...rest } = args;
    const rows = await ctx.db
      .query("partTranscripts")
      .withIndex("by_sha256_config", (q) =>
        q.eq("sha256", sha256).eq("configHash", configHash),
      )
      .collect();
    const existing = exactlyZeroOrOne(
      rows,
      `partTranscripts.(${sha256},${configHash})`,
    );
    const patch = {
      status,
      ...rest,
      ...(status === "processing" ? { processingStartedAt: Date.now() } : {}),
    };
    if (existing === null) {
      const id = await ctx.db.insert("partTranscripts", {
        sha256,
        configHash,
        attempts: status === "processing" ? 1 : 0,
        ...patch,
      });
      return { id, created: true };
    }
    await ctx.db.patch(existing._id, {
      ...patch,
      attempts: existing.attempts + (status === "processing" ? 1 : 0),
    });
    return { id: existing._id, created: false };
  },
});

export const upsertLessonByKey = mutation({
  args: {
    lessonKey: v.string(),
    assemblyHash: v.string(),
    channelId: v.optional(v.id("channels")),
    rawTitle: v.string(),
    normalizedTitle: v.string(),
    seriesName: v.optional(v.string()),
    normalizedSeriesName: v.optional(v.string()),
    seriesEpisode: v.optional(v.number()),
    lessonPartLabel: v.optional(v.string()),
    titleParserVersion: v.string(),
    titleParseConfidence: v.number(),
    groupingVersion: v.string(),
    groupingConfidence: v.number(),
    reviewStatus: v.union(v.literal("auto"), v.literal("needs_review")),
    titleMessageId: v.optional(v.id("telegramMessages")),
    firstTelegramMessageId: v.optional(v.id("telegramMessages")),
    lastTelegramMessageId: v.optional(v.id("telegramMessages")),
    partCount: v.number(),
    durationMs: v.number(),
  },
  handler: async (ctx, args) => {
    const rows = await ctx.db
      .query("lessons")
      .withIndex("by_lesson_key", (q) => q.eq("lessonKey", args.lessonKey))
      .collect();
    const existing = exactlyZeroOrOne(rows, `lessons.lessonKey=${args.lessonKey}`);
    if (existing === null) {
      const id = await ctx.db.insert("lessons", { ...args, mergeStatus: "pending" });
      return { id, created: true, skipped: false };
    }
    // §0 amendment 1 / §4.7: an approved composition is frozen against reruns.
    // A source change demotes it to needs_review elsewhere; it is never
    // silently recomposed here.
    if (existing.reviewStatus === "approved") {
      return { id: existing._id, created: false, skipped: true };
    }
    const { reviewStatus, ...composition } = args;
    await ctx.db.patch(existing._id, {
      ...composition,
      reviewStatus,
      // A new composition invalidates the merged artifact.
      ...(existing.assemblyHash !== args.assemblyHash
        ? { mergeStatus: "pending" as const }
        : {}),
    });
    return { id: existing._id, created: false, skipped: false };
  },
});

export const acquirePipelineStage = mutation({
  args: { stage: v.string(), runId: v.string(), owner: v.string() },
  handler: async (ctx, args) => {
    const rows = await ctx.db
      .query("pipelineLocks")
      .withIndex("by_stage", (q) => q.eq("stage", args.stage))
      .collect();
    const existing = exactlyZeroOrOne(rows, `pipelineLocks.stage=${args.stage}`);
    const now = Date.now();
    if (existing !== null && now - existing.heartbeatAt <= STALE_HEARTBEAT_MS) {
      return { acquired: false, holder: existing.runId, owner: existing.owner };
    }
    if (existing !== null) {
      await ctx.db.patch(existing._id, {
        runId: args.runId,
        owner: args.owner,
        acquiredAt: now,
        heartbeatAt: now,
      });
      return { acquired: true, holder: args.runId, stolenFrom: existing.runId };
    }
    await ctx.db.insert("pipelineLocks", {
      stage: args.stage,
      runId: args.runId,
      owner: args.owner,
      acquiredAt: now,
      heartbeatAt: now,
    });
    return { acquired: true, holder: args.runId };
  },
});

export const heartbeatPipelineStage = mutation({
  args: { stage: v.string(), runId: v.string() },
  handler: async (ctx, args) => {
    const rows = await ctx.db
      .query("pipelineLocks")
      .withIndex("by_stage", (q) => q.eq("stage", args.stage))
      .collect();
    const existing = exactlyZeroOrOne(rows, `pipelineLocks.stage=${args.stage}`);
    if (existing === null || existing.runId !== args.runId) {
      return { held: false };
    }
    await ctx.db.patch(existing._id, { heartbeatAt: Date.now() });
    return { held: true };
  },
});

export const releasePipelineStage = mutation({
  args: { stage: v.string(), runId: v.string() },
  handler: async (ctx, args) => {
    const rows = await ctx.db
      .query("pipelineLocks")
      .withIndex("by_stage", (q) => q.eq("stage", args.stage))
      .collect();
    const existing = exactlyZeroOrOne(rows, `pipelineLocks.stage=${args.stage}`);
    // Only the holder releases: a run that already lost a stale lock must not
    // delete the new holder's row.
    if (existing === null || existing.runId !== args.runId) {
      return { released: false };
    }
    await ctx.db.delete(existing._id);
    return { released: true };
  },
});
