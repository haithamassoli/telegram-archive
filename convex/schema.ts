// Convex schema — plan §2, verbatim. Uniqueness on sha256 / (sha256,configHash) /
// lessonKey / stage is NOT enforced by these indexes; it is enforced by the atomic
// mutations in mutations.ts, which are serializable (plan §0 item 3).
import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";

export default defineSchema({
  channels: defineTable({
    username: v.string(),
    title: v.string(),
    lastMessageId: v.number(),
    lastSyncAt: v.number(),
  }).index("by_username", ["username"]),

  telegramMessages: defineTable({
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
    semanticType: v.union(
      v.literal("article"),
      v.literal("lesson_title"),
      v.literal("link"),
      v.literal("notice"),
      v.literal("other"),
      v.null(),
    ),
    isForwarded: v.boolean(),
    forwardedFromChannel: v.optional(v.string()),
    forwardedFromMsgId: v.optional(v.number()),
    classifierVersion: v.optional(v.string()),
  })
    .index("by_channel_message", ["channelId", "telegramMessageId"])
    .index("by_channel_date", ["channelId", "date"]),

  // One row per unique binary. Identity = sha256.
  mediaObjects: defineTable({
    sha256: v.string(),
    r2Key: v.string(),
    ext: v.string(),
    sizeBytes: v.number(),
    mimeType: v.optional(v.string()),
    durationMs: v.optional(v.number()),
    codec: v.optional(v.string()),
    sampleRate: v.optional(v.number()),
    channelCount: v.optional(v.number()),
    firstSeenAt: v.number(),
  }).index("by_sha256", ["sha256"]),

  // Occurrence -> binary. A repost adds a row here only.
  messageMedia: defineTable({
    messageId: v.id("telegramMessages"),
    mediaObjectId: v.id("mediaObjects"),
    // MTProto document.id — reference only; the durable re-download path is
    // (channel, msgId).
    telegramDocId: v.optional(v.string()),
    originalFileName: v.optional(v.string()),
  })
    .index("by_message", ["messageId"])
    .index("by_media_object", ["mediaObjectId"]),

  partTranscripts: defineTable({
    sha256: v.string(),
    configHash: v.string(),
    status: v.union(
      v.literal("pending"),
      v.literal("processing"),
      v.literal("done"),
      v.literal("failed"),
    ),
    processingStartedAt: v.optional(v.number()),
    processingRunId: v.optional(v.string()),
    attempts: v.number(),
    model: v.optional(v.string()),
    modelRevision: v.optional(v.string()),
    rawR2Key: v.optional(v.string()),
    durationMs: v.optional(v.number()),
    segmentCount: v.optional(v.number()),
    error: v.optional(v.string()),
  }).index("by_sha256_config", ["sha256", "configHash"]),

  articles: defineTable({
    messageId: v.id("telegramMessages"),
    channelId: v.id("channels"),
    title: v.string(),
    normalizedTitle: v.string(),
    titleSource: v.string(),
    text: v.string(),
    normalizedText: v.string(),
    date: v.number(),
    telegramUrl: v.string(),
    indexedAt: v.optional(v.number()),
    indexVersion: v.optional(v.string()),
  })
    .index("by_message", ["messageId"])
    .index("by_channel_date", ["channelId", "date"]),

  lessons: defineTable({
    // Deterministic from channel + title msgId + first source msgId.
    lessonKey: v.string(),
    // Ordered part sha256s only (§0 amendment 3).
    assemblyHash: v.string(),
    channelId: v.optional(v.id("channels")), // null for non-Telegram sources
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
    reviewStatus: v.union(
      v.literal("auto"),
      v.literal("needs_review"),
      v.literal("approved"),
    ),
    mergeStatus: v.union(
      v.literal("pending"),
      v.literal("processing"),
      v.literal("done"),
      v.literal("failed"),
    ),
    mergedR2Key: v.optional(v.string()),
    mergedSha256: v.optional(v.string()),
    lessonTranscriptR2Key: v.optional(v.string()),
    indexedAt: v.optional(v.number()),
    indexVersion: v.optional(v.string()),
    titleMessageId: v.optional(v.id("telegramMessages")),
    firstTelegramMessageId: v.optional(v.id("telegramMessages")),
    lastTelegramMessageId: v.optional(v.id("telegramMessages")),
    partCount: v.number(),
    durationMs: v.number(),
  })
    .index("by_lesson_key", ["lessonKey"])
    .index("by_review_status", ["reviewStatus"]),

  lessonParts: defineTable({
    lessonId: v.id("lessons"),
    messageId: v.optional(v.id("telegramMessages")),
    mediaObjectId: v.id("mediaObjects"),
    order: v.number(),
    durationMs: v.number(),
    offsetMs: v.number(),
  })
    .index("by_lesson_order", ["lessonId", "order"])
    .index("by_message", ["messageId"]),

  lessonSources: defineTable({
    lessonId: v.id("lessons"),
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
    metadata: v.optional(v.any()),
  }).index("by_lesson", ["lessonId"]),

  failures: defineTable({
    stage: v.string(),
    refKey: v.string(),
    error: v.string(),
    attempts: v.number(),
    lastTriedAt: v.number(),
    resolved: v.boolean(),
  })
    .index("by_stage_resolved", ["stage", "resolved"])
    .index("by_stage_ref", ["stage", "refKey"]),

  // Singleton per stage; uniqueness via acquirePipelineStage.
  pipelineLocks: defineTable({
    stage: v.string(),
    runId: v.string(),
    acquiredAt: v.number(),
    heartbeatAt: v.number(),
    owner: v.string(),
  }).index("by_stage", ["stage"]),

  // Append-only history.
  pipelineRuns: defineTable({
    runId: v.string(),
    stage: v.string(),
    startedAt: v.number(),
    finishedAt: v.optional(v.number()),
    status: v.string(),
    processedCount: v.number(),
    successCount: v.number(),
    failureCount: v.number(),
    skippedCount: v.number(),
    wallTimeMs: v.optional(v.number()),
    audioDurationMs: v.optional(v.number()),
    summary: v.optional(v.string()),
  })
    .index("by_stage_started", ["stage", "startedAt"])
    .index("by_run", ["runId"]),
});
