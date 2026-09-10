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
    const existing = exactlyZeroOrOne(
      rows,
      `mediaObjects.sha256=${args.sha256}`,
    );
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
      // A row that succeeded on retry must not keep the old failure text, and a
      // row that failed must not keep pointing at an artifact that is not there.
      // `undefined` in a patch removes the field.
      ...(status === "done" ? { error: undefined } : {}),
      ...(status === "failed" && rest.rawR2Key === undefined
        ? { rawR2Key: undefined }
        : {}),
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
    const existing = exactlyZeroOrOne(
      rows,
      `lessons.lessonKey=${args.lessonKey}`,
    );
    if (existing === null) {
      const id = await ctx.db.insert("lessons", {
        ...args,
        mergeStatus: "pending",
      });
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
    const existing = exactlyZeroOrOne(
      rows,
      `pipelineLocks.stage=${args.stage}`,
    );
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
    const existing = exactlyZeroOrOne(
      rows,
      `pipelineLocks.stage=${args.stage}`,
    );
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
    const existing = exactlyZeroOrOne(
      rows,
      `pipelineLocks.stage=${args.stage}`,
    );
    // Only the holder releases: a run that already lost a stale lock must not
    // delete the new holder's row.
    if (existing === null || existing.runId !== args.runId) {
      return { released: false };
    }
    await ctx.db.delete(existing._id);
    return { released: true };
  },
});

// ---------------------------------------------------------------------------
// M1 — archive everything (plan Phase 1). Message/channel/media-link upserts,
// run history, and the failures ledger the batch harness drains (§4.5, §4.6).
// ---------------------------------------------------------------------------

export const upsertChannel = mutation({
  args: { username: v.string(), title: v.string() },
  handler: async (ctx, args) => {
    const rows = await ctx.db
      .query("channels")
      .withIndex("by_username", (q) => q.eq("username", args.username))
      .collect();
    const existing = exactlyZeroOrOne(
      rows,
      `channels.username=${args.username}`,
    );
    if (existing !== null) {
      if (existing.title !== args.title) {
        await ctx.db.patch(existing._id, { title: args.title });
      }
      return {
        id: existing._id,
        lastMessageId: existing.lastMessageId,
        created: false,
      };
    }
    const id = await ctx.db.insert("channels", {
      ...args,
      lastMessageId: 0,
      lastSyncAt: Date.now(),
    });
    return { id, lastMessageId: 0, created: true };
  },
});

// The checkpoint is a high-water mark: a resumed run replays the tail of an
// unflushed batch, and must never rewind what an earlier run already proved.
export const checkpointChannel = mutation({
  args: { channelId: v.id("channels"), lastMessageId: v.number() },
  handler: async (ctx, { channelId, lastMessageId }) => {
    const channel = await ctx.db.get(channelId);
    if (channel === null) throw new Error(`no channel ${channelId}`);
    const next = Math.max(channel.lastMessageId, lastMessageId);
    await ctx.db.patch(channelId, {
      lastMessageId: next,
      lastSyncAt: Date.now(),
    });
    return { lastMessageId: next };
  },
});

export const upsertTelegramMessage = mutation({
  args: {
    channelId: v.id("channels"),
    telegramMessageId: v.number(),
    date: v.number(),
    editDate: v.optional(v.number()),
    deletedAt: v.optional(v.number()),
    text: v.optional(v.string()),
    replyToMessageId: v.optional(v.number()),
    groupedId: v.optional(v.string()),
    telegramUrl: v.string(),
    mediaType: v.union(
      v.literal("none"),
      v.literal("audio"),
      v.literal("voice"),
      v.literal("video"),
      v.literal("photo"),
      v.literal("document"),
    ),
    isForwarded: v.boolean(),
    forwardedFromChannel: v.optional(v.string()),
    forwardedFromMsgId: v.optional(v.number()),
  },
  handler: async (ctx, args) => {
    const rows = await ctx.db
      .query("telegramMessages")
      .withIndex("by_channel_message", (q) =>
        q
          .eq("channelId", args.channelId)
          .eq("telegramMessageId", args.telegramMessageId),
      )
      .collect();
    const existing = exactlyZeroOrOne(
      rows,
      `telegramMessages.(${args.channelId},${args.telegramMessageId})`,
    );
    if (existing === null) {
      // semanticType/classifierVersion belong to the Organizer (Phase 3); ingest
      // only ever writes null here.
      const id = await ctx.db.insert("telegramMessages", {
        ...args,
        semanticType: null,
      });
      return { id, created: true };
    }
    // Patch the ingest-owned fields only. Absent optionals are `undefined`,
    // which clears them — so an edit that dropped a reply is reflected, and
    // semanticType/classifierVersion survive a re-ingest untouched.
    await ctx.db.patch(existing._id, {
      date: args.date,
      editDate: args.editDate,
      deletedAt: args.deletedAt,
      text: args.text,
      replyToMessageId: args.replyToMessageId,
      groupedId: args.groupedId,
      telegramUrl: args.telegramUrl,
      mediaType: args.mediaType,
      isForwarded: args.isForwarded,
      forwardedFromChannel: args.forwardedFromChannel,
      forwardedFromMsgId: args.forwardedFromMsgId,
    });
    return { id: existing._id, created: false };
  },
});

export const linkMessageMedia = mutation({
  args: {
    messageId: v.id("telegramMessages"),
    mediaObjectId: v.id("mediaObjects"),
    telegramDocId: v.optional(v.string()),
    originalFileName: v.optional(v.string()),
  },
  handler: async (ctx, args) => {
    const rows = await ctx.db
      .query("messageMedia")
      .withIndex("by_message", (q) => q.eq("messageId", args.messageId))
      .collect();
    const existing =
      rows.find((row) => row.mediaObjectId === args.mediaObjectId) ?? null;
    if (existing !== null) return { id: existing._id, created: false };
    const id = await ctx.db.insert("messageMedia", args);
    return { id, created: true };
  },
});

export const startPipelineRun = mutation({
  args: { runId: v.string(), stage: v.string() },
  handler: async (ctx, args) => {
    // The Python client retries on a lost response, so this has to be an upsert:
    // two rows for one runId would make finishPipelineRun throw and lose the
    // whole run's history.
    const rows = await ctx.db
      .query("pipelineRuns")
      .withIndex("by_run", (q) => q.eq("runId", args.runId))
      .collect();
    const existing = exactlyZeroOrOne(rows, `pipelineRuns.runId=${args.runId}`);
    if (existing !== null) return { id: existing._id };
    const id = await ctx.db.insert("pipelineRuns", {
      ...args,
      startedAt: Date.now(),
      status: "running",
      processedCount: 0,
      successCount: 0,
      failureCount: 0,
      skippedCount: 0,
    });
    return { id };
  },
});

export const finishPipelineRun = mutation({
  args: {
    runId: v.string(),
    status: v.string(),
    processedCount: v.number(),
    successCount: v.number(),
    failureCount: v.number(),
    skippedCount: v.number(),
    audioDurationMs: v.optional(v.number()),
    summary: v.optional(v.string()),
  },
  handler: async (ctx, { runId, ...counts }) => {
    const rows = await ctx.db
      .query("pipelineRuns")
      .withIndex("by_run", (q) => q.eq("runId", runId))
      .collect();
    const existing = exactlyZeroOrOne(rows, `pipelineRuns.runId=${runId}`);
    if (existing === null) throw new Error(`no pipelineRuns row for ${runId}`);
    const finishedAt = Date.now();
    await ctx.db.patch(existing._id, {
      ...counts,
      finishedAt,
      wallTimeMs: finishedAt - existing.startedAt,
    });
    return { id: existing._id };
  },
});

export const recordFailure = mutation({
  args: { stage: v.string(), refKey: v.string(), error: v.string() },
  handler: async (ctx, args) => {
    const rows = await ctx.db
      .query("failures")
      .withIndex("by_stage_ref", (q) =>
        q.eq("stage", args.stage).eq("refKey", args.refKey),
      )
      .collect();
    const existing = exactlyZeroOrOne(
      rows,
      `failures.(${args.stage},${args.refKey})`,
    );
    if (existing === null) {
      const id = await ctx.db.insert("failures", {
        ...args,
        attempts: 1,
        lastTriedAt: Date.now(),
        resolved: false,
      });
      return { id, attempts: 1 };
    }
    const attempts = existing.attempts + 1;
    await ctx.db.patch(existing._id, {
      error: args.error,
      attempts,
      lastTriedAt: Date.now(),
      resolved: false,
    });
    return { id: existing._id, attempts };
  },
});

export const resolveFailure = mutation({
  args: { stage: v.string(), refKey: v.string() },
  handler: async (ctx, args) => {
    const rows = await ctx.db
      .query("failures")
      .withIndex("by_stage_ref", (q) =>
        q.eq("stage", args.stage).eq("refKey", args.refKey),
      )
      .collect();
    const existing = exactlyZeroOrOne(
      rows,
      `failures.(${args.stage},${args.refKey})`,
    );
    if (existing === null) return { resolved: false };
    await ctx.db.patch(existing._id, {
      resolved: true,
      lastTriedAt: Date.now(),
    });
    return { resolved: true };
  },
});

// --------------------------------------------------------------------------- //
// M3 — Organizer (Phase 3) and Lesson Transcript Builder (Phase 3.5)
//
// Every mutation below reports whether it actually changed anything. That is
// not decoration: M3's exit criterion is "a full Organizer re-run on unchanged
// data is a no-op", and `changed` is how the run proves it.
// --------------------------------------------------------------------------- //

const semanticTypeValue = v.union(
  v.literal("article"),
  v.literal("lesson_title"),
  v.literal("link"),
  v.literal("notice"),
  v.literal("other"),
  v.null(),
);

export const classifyMessages = mutation({
  args: {
    updates: v.array(
      v.object({
        messageId: v.id("telegramMessages"),
        semanticType: semanticTypeValue,
        classifierVersion: v.string(),
      }),
    ),
  },
  handler: async (ctx, args) => {
    let changed = 0;
    for (const update of args.updates) {
      const row = await ctx.db.get(update.messageId);
      if (row === null) {
        throw new Error(`no telegramMessage ${update.messageId}`);
      }
      if (
        row.semanticType === update.semanticType &&
        row.classifierVersion === update.classifierVersion
      ) {
        continue;
      }
      await ctx.db.patch(update.messageId, {
        semanticType: update.semanticType,
        classifierVersion: update.classifierVersion,
      });
      changed += 1;
    }
    return { changed };
  },
});

export const upsertArticle = mutation({
  args: {
    messageId: v.id("telegramMessages"),
    channelId: v.id("channels"),
    title: v.string(),
    normalizedTitle: v.string(),
    titleSource: v.string(),
    text: v.string(),
    normalizedText: v.string(),
    date: v.number(),
    telegramUrl: v.string(),
  },
  handler: async (ctx, args) => {
    const rows = await ctx.db
      .query("articles")
      .withIndex("by_message", (q) => q.eq("messageId", args.messageId))
      .collect();
    const existing = exactlyZeroOrOne(
      rows,
      `articles.messageId=${args.messageId}`,
    );
    if (existing === null) {
      const id = await ctx.db.insert("articles", args);
      return { id, created: true, changed: true };
    }
    const same = (Object.keys(args) as (keyof typeof args)[]).every(
      (key) => existing[key as keyof typeof existing] === args[key],
    );
    if (same) {
      return { id: existing._id, created: false, changed: false };
    }
    // indexedAt/indexVersion belong to Phase 4 and are left untouched here: a
    // changed article is reindexed because its text moved, not because M3 ran.
    await ctx.db.patch(existing._id, args);
    return { id: existing._id, created: false, changed: true };
  },
});

// Parts are a set, not independently upsertable rows: a rerun that drops a part
// must not leave it orphaned under the lesson. Replace-all in one transaction,
// and only when the set actually differs.
export const replaceLessonParts = mutation({
  args: {
    lessonId: v.id("lessons"),
    parts: v.array(
      v.object({
        messageId: v.optional(v.id("telegramMessages")),
        mediaObjectId: v.id("mediaObjects"),
        order: v.number(),
        durationMs: v.number(),
        offsetMs: v.number(),
      }),
    ),
  },
  handler: async (ctx, args) => {
    const existing = await ctx.db
      .query("lessonParts")
      .withIndex("by_lesson_order", (q) => q.eq("lessonId", args.lessonId))
      .collect();
    existing.sort((a, b) => a.order - b.order);
    const wanted = [...args.parts].sort((a, b) => a.order - b.order);
    const same =
      existing.length === wanted.length &&
      existing.every(
        (row, i) =>
          row.mediaObjectId === wanted[i].mediaObjectId &&
          row.order === wanted[i].order &&
          row.offsetMs === wanted[i].offsetMs &&
          row.durationMs === wanted[i].durationMs &&
          (row.messageId ?? null) === (wanted[i].messageId ?? null),
      );
    if (same) {
      return { changed: false, count: existing.length };
    }
    for (const row of existing) {
      await ctx.db.delete(row._id);
    }
    for (const part of wanted) {
      await ctx.db.insert("lessonParts", { lessonId: args.lessonId, ...part });
    }
    return { changed: true, count: wanted.length };
  },
});

export const replaceLessonSources = mutation({
  args: {
    lessonId: v.id("lessons"),
    sources: v.array(
      v.object({
        sourceType: v.union(
          v.literal("telegram"),
          v.literal("youtube"),
          v.literal("legacy"),
        ),
        url: v.string(),
        externalId: v.optional(v.string()),
        channelId: v.optional(v.id("channels")),
        messageId: v.optional(v.id("telegramMessages")),
        isPrimary: v.boolean(),
      }),
    ),
  },
  handler: async (ctx, args) => {
    const existing = await ctx.db
      .query("lessonSources")
      .withIndex("by_lesson", (q) => q.eq("lessonId", args.lessonId))
      .collect();
    const key = (row: { sourceType: string; url: string }) =>
      `${row.sourceType} ${row.url}`;
    const before = existing.map(key).sort().join("\n");
    const after = args.sources.map(key).sort().join("\n");
    if (before === after) {
      return { changed: false, count: existing.length };
    }
    for (const row of existing) {
      await ctx.db.delete(row._id);
    }
    for (const source of args.sources) {
      await ctx.db.insert("lessonSources", {
        lessonId: args.lessonId,
        ...source,
      });
    }
    return { changed: true, count: args.sources.length };
  },
});

// Phase 3.5, second half of the write-order law: the artifact is in R2 before
// this pointer moves. Kept out of `upsertLessonByKey` so the builder can obey
// that law without re-sending a whole composition.
export const setLessonTranscript = mutation({
  args: {
    lessonId: v.id("lessons"),
    lessonTranscriptR2Key: v.string(),
  },
  handler: async (ctx, args) => {
    const row = await ctx.db.get(args.lessonId);
    if (row === null) {
      throw new Error(`no lesson ${args.lessonId}`);
    }
    if (row.lessonTranscriptR2Key === args.lessonTranscriptR2Key) {
      return { changed: false };
    }
    await ctx.db.patch(args.lessonId, {
      lessonTranscriptR2Key: args.lessonTranscriptR2Key,
    });
    return { changed: true };
  },
});

// §0 amendment 1 / §4.7's other half. An approved lesson is frozen against
// recomposition, but a source message edited or deleted underneath it must not
// be silently kept either. The only legal move is to hand it back to a human.
export const demoteLesson = mutation({
  args: { lessonId: v.id("lessons"), reason: v.string() },
  handler: async (ctx, args) => {
    const row = await ctx.db.get(args.lessonId);
    if (row === null) {
      throw new Error(`no lesson ${args.lessonId}`);
    }
    if (row.reviewStatus !== "approved") {
      return { demoted: false };
    }
    await ctx.db.patch(args.lessonId, { reviewStatus: "needs_review" });
    return { demoted: true };
  },
});
